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


async def run_ccma(flow, *, chat_id, state_key, credential_line, credential_sha256, period_from, period_to, client_slug, client_cuit):
    from plugins.platforms.telegram.ccma_artifact import destination, publish
    clients_root = Path(os.environ.get('CONTABOT_CLIENTES_ROOT', Path.home() / 'clientes'))
    destination(clients_root, client_slug, client_cuit, period_from, period_to)
    home = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
    probe = home / 'skills/productivity/ccma-obligaciones-pagos/scripts/arca_ccma_probe.js'
    builder = Path(__file__).with_name('ccma_workbook.py')
    python = getattr(flow.catalog, 'runtime_python', None)
    node = shutil.which('node')
    if not node or not probe.is_file() or not python or not Path(python).is_file():
        await flow.send(chat_id, 'CCMA no pudo iniciarse: falta un componente del procedimiento.')
        return
    root = Path.home() / 'hermes-workspace/output/private/ccma-runs'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = Path(tempfile.mkdtemp(prefix='consulta-', dir=root))
    source, workbook = run_dir / 'fuente.csv', run_dir / 'ccma_obligaciones_pagos.xlsx'
    process = None
    credential_copy = run_dir / 'access.csv'
    credential_frozen = False
    try:
        # Freeze the exact locally selected credential version for this runner.
        original = Path(os.environ.get('ARCA_CSV_FILE', home / '.arca.csv'))
        freeze_credentials(original, credential_copy, credential_sha256)
        credential_frozen = True
        env = {'HOME': str(Path.home()), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
               'ARCA_CSV_FILE': str(credential_copy), 'ARCA_CSV_LINE': str(credential_line),
               'ARCA_PERIOD_FROM': period_from, 'ARCA_PERIOD_TO': period_to,
               'ARCA_EXPORT_FILE': str(source)}
        process = await asyncio.create_subprocess_exec(node, str(probe), cwd=str(probe.parent), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        flow._sct_dispatch_processes[state_key] = process
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=240)
        status = flow._sct_runner_status(stdout)
        if process.returncode != 0 or status != 'source_copied' or not source.is_file() or source.is_symlink():
            await flow.send(chat_id, 'CCMA no completó la consulta a ARCA. No se generó ni se reenvió un libro anterior.')
            return
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        reported = re.search(rb'^source_sha256=([a-f0-9]{64})$', stdout, re.MULTILINE)
        if not reported or reported.group(1).decode() != digest:
            raise ValueError('source_integrity')
        source.chmod(0o600)
        await terminate_owned_group(process)
        process = None
        env = {'HOME': str(Path.home()), 'PATH': os.environ.get('PATH','/usr/bin:/bin'), 'LANG':'C.UTF-8',
               'CCMA_SOURCE_FILE': str(source), 'CCMA_WORKBOOK_FILE': str(workbook),
               'CCMA_PERIOD_LABEL': f'{period_from}–{period_to}', 'CCMA_SOURCE_SHA256': digest,
               'CCMA_GENERATED_AT': datetime.now(timezone.utc).isoformat()}
        process = await asyncio.create_subprocess_exec(str(python), str(builder), env=env,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        flow._sct_dispatch_processes[state_key] = process
        await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode != 0 or not workbook.is_file() or workbook.is_symlink():
            await flow.send(chat_id, 'CCMA obtuvo una fuente nueva, pero falló la generación del Excel. No se entregó un archivo anterior.')
            return
        workbook.chmod(0o600)
        workbook = await asyncio.to_thread(publish, workbook, clients_root, client_slug, client_cuit, period_from, period_to)
        delivery = await flow.send_document(chat_id=chat_id, file_path=str(workbook),
                    file_name=workbook.name, caption=f'CCMA — consulta nueva de {period_from} a {period_to}.')
        await flow.send(chat_id, 'Consulta CCMA finalizada. Se entregó el Excel de esta ejecución.' if delivery.success
                        else 'La consulta CCMA finalizó, pero Telegram no pudo entregar el Excel.')
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        await flow.send(chat_id, 'CCMA superó el tiempo máximo. No se entregó ningún libro anterior.')
    except ValueError:
        await flow.send(chat_id, 'CCMA no pudo verificar la integridad del acceso o de la fuente. Iniciá una consulta nueva.')
    except Exception:
        await flow.send(chat_id, 'CCMA no pudo completar el procedimiento. No se entregó ningún libro anterior.')
    finally:
        try:
            await terminate_owned_group(process)
        finally:
            if credential_frozen:
                credential_copy.unlink(missing_ok=True)
            flow._sct_dispatch_processes.pop(state_key, None)
