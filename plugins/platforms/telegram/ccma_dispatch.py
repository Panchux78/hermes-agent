"""Bounded CCMA consultation and workbook delivery, without an agent turn."""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone
from plugins.platforms.telegram.fiscal_execution import freeze_credentials, terminate_owned_group
from plugins.platforms.telegram.fiscal_runtime import browser_environment, require_fiscal_runtime, unavailable_message
from plugins.platforms.telegram.fiscal_credentials import canonical_access, FiscalDatabaseError
from plugins.platforms.telegram.fiscal_interaction import communicate as interactive_communicate
from plugins.platforms.telegram.ccma_diagnostics import Diagnostics


async def run_ccma(flow, *, chat_id, state_key, credential_line, credential_sha256, period_from, period_to, client_slug, client_cuit, contributor_id=None, holder_cuit=None):
    from plugins.platforms.telegram.ccma_artifact import destination, publish
    clients_root = Path(os.environ.get('CONTABOT_CLIENTES_ROOT', Path.home() / 'clientes'))
    destination(clients_root, client_slug, client_cuit, period_from, period_to)
    home = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
    probe = home / 'skills/productivity/ccma-obligaciones-pagos/scripts/arca_ccma_probe.js'
    builder = Path(__file__).with_name('ccma_workbook.py')
    python = getattr(flow.catalog, 'runtime_python', None)
    node = shutil.which('node')
    root = Path.home() / 'hermes-workspace/output/private/ccma-runs'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = Path(tempfile.mkdtemp(prefix='consulta-', dir=root))
    diagnostic = Diagnostics(run_dir)
    diagnostic.record({'stage': 'preflight'})
    try:
        await require_fiscal_runtime(python, node, probe, home, canonical=contributor_id is not None)
    except RuntimeError as error:
        diagnostic.failure(str(error))
        diagnostic.close()
        await flow.send(chat_id, unavailable_message('CCMA', str(error)) + f' Referencia: {run_dir.name}.')
        return
    except BaseException:
        diagnostic.close()
        raise
    source, workbook = run_dir / 'fuente.csv', run_dir / 'ccma_obligaciones_pagos.xlsx'
    process = None
    credential_copy = run_dir / 'access.csv'
    credential_frozen = False
    try:
        # Freeze the exact locally selected credential version for this runner.
        initial = None
        diagnostic.record({'stage': 'access'})
        if contributor_id is not None:
            initial = await canonical_access(contributor_id, client_cuit, client_slug, holder_cuit)
        else:
            original = Path(os.environ.get('ARCA_CSV_FILE', home / '.arca.csv'))
            freeze_credentials(original, credential_copy, credential_sha256)
            credential_frozen = True
        env = {**browser_environment(home, python=python),
               'ARCA_CSV_FILE': str(credential_copy), 'ARCA_CSV_LINE': str(credential_line),
               'ARCA_PERIOD_FROM': period_from, 'ARCA_PERIOD_TO': period_to,
               'ARCA_EXPORT_FILE': str(source)}
        if contributor_id is not None:
            env.pop('ARCA_CSV_FILE'); env.pop('ARCA_CSV_LINE')
            env['FISCAL_CREDENTIAL_STDIN'] = '1'
        env['FISCAL_CAPTCHA_DIR'] = str(run_dir)
        diagnostic.record({'stage': 'browser'})
        process = await asyncio.create_subprocess_exec(node, str(probe), cwd=str(probe.parent), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE, start_new_session=True)
        flow._sct_dispatch_processes[state_key] = process
        stdout = await interactive_communicate(flow, process, state_key, chat_id, run_dir, initial, on_diagnostic=diagnostic.record)
        initial = None
        status = flow._sct_runner_status(stdout)
        if process.returncode != 0 or status != 'source_copied' or not source.is_file() or source.is_symlink():
            code = status if status != 'source_copied' else ('runner_exit_error' if process.returncode else 'source_missing')
            await flow.send(chat_id, diagnostic.failure(code, process.returncode))
            return
        diagnostic.record({'stage': 'source'})
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        reported = re.search(rb'^source_sha256=([a-f0-9]{64})$', stdout, re.MULTILINE)
        if not reported or reported.group(1).decode() != digest:
            raise ValueError('source_integrity')
        source.chmod(0o600)
        await terminate_owned_group(process)
        process = None
        diagnostic.record({'stage': 'workbook'})
        env = {'HOME': str(Path.home()), 'PATH': os.environ.get('PATH','/usr/bin:/bin'), 'LANG':'C.UTF-8',
               'CCMA_SOURCE_FILE': str(source), 'CCMA_WORKBOOK_FILE': str(workbook),
               'CCMA_PERIOD_LABEL': f'{period_from}–{period_to}', 'CCMA_SOURCE_SHA256': digest,
               'CCMA_GENERATED_AT': datetime.now(timezone.utc).isoformat()}
        process = await asyncio.create_subprocess_exec(str(python), str(builder), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        flow._sct_dispatch_processes[state_key] = process
        builder_stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode != 0 or not workbook.is_file() or workbook.is_symlink():
            text = ('CCMA: la fuente tiene un importe ilegible; no se generó el libro. La fuente quedó preservada para revisión.'
                    if builder_stdout.strip() == b'result=ccma_amount_unreadable'
                    else 'CCMA obtuvo una fuente nueva, pero falló la generación del Excel. No se entregó un archivo anterior.')
            diagnostic.failure('ccma_amount_unreadable' if builder_stdout.strip() == b'result=ccma_amount_unreadable' else 'workbook_failed', process.returncode)
            await flow.send(chat_id, text + f' Referencia: {run_dir.name}.')
            return
        workbook.chmod(0o600)
        workbook = await asyncio.to_thread(publish, workbook, clients_root, client_slug, client_cuit, period_from, period_to)
        diagnostic.record({'stage': 'delivery'})
        delivery = await flow.send_document(chat_id=chat_id, file_path=str(workbook),
                    file_name=workbook.name, caption=f'CCMA — consulta nueva de {period_from} a {period_to}.')
        diagnostic.record({'code': 'complete' if delivery.success else 'delivery_failed'})
        await flow.send(chat_id, 'Consulta CCMA finalizada. Se entregó el Excel de esta ejecución.' if delivery.success
                        else 'La consulta CCMA finalizó, pero Telegram no pudo entregar el Excel.')
    except asyncio.CancelledError:
        diagnostic.record({'code': 'cancelled'})
        raise
    except asyncio.TimeoutError:
        await flow.send(chat_id, diagnostic.failure('timeout'))
    except FiscalDatabaseError as error:
        diagnostic.failure(str(error))
        await flow.send(chat_id, unavailable_message('CCMA', str(error)) + f' Referencia: {run_dir.name}.')
    except ValueError as error:
        await flow.send(chat_id, diagnostic.failure(str(error), error=error))
    except Exception as error:
        await flow.send(chat_id, diagnostic.failure('unexpected_error', error=error))
    finally:
        try:
            await terminate_owned_group(process)
        finally:
            if credential_frozen:
                credential_copy.unlink(missing_ok=True)
            flow._sct_dispatch_processes.pop(state_key, None)
            diagnostic.close()
