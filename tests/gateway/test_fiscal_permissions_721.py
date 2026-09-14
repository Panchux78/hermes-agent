"""Database failures must stop both buttons before ARCA, with actionable diagnostics."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from plugins.platforms.telegram import fiscal_credentials as credentials
from plugins.platforms.telegram import fiscal_runtime, fiscal_query_flow, ccma_dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize('result,code', [
    ('ok', None), ('denied', 'fiscal_database_permissions'),
    ('down', 'fiscal_database_unavailable'), ('admin', 'fiscal_database_profile_invalid'),
])
async def test_sql_preflight_reports_only_safe_codes(monkeypatch, caplog, result, code):
    calls = []
    def invocation(capability):
        calls.append(capability)
        output = b'fiscal_sql_ready\nQuery Plan\n' if result == 'ok' else b'Query Plan\n'
        error = b'ERROR: 42501\n' if result == 'denied' else b'private diagnostic must not escape'
        script = f'import sys;sys.stdout.buffer.write({output!r});sys.stderr.buffer.write({error!r});sys.exit({int(result in ("denied","down"))})'
        return [sys.executable, '-c', script], {}
    monkeypatch.setitem(sys.modules, 'contabot_pg', NS(psql_invocation=invocation))
    if code:
        with pytest.raises(credentials.FiscalDatabaseError, match=code):
            await credentials.require_fiscal_database()
        assert code in caplog.text
    else:
        await credentials.require_fiscal_database()
    assert calls == ['fiscal']
    assert 'private diagnostic' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['ccma', 'sct'])
@pytest.mark.parametrize('failure_stage', ['preflight', 'access'])
async def test_missing_sql_permission_never_starts_portal_or_suggests_retry(tmp_path, monkeypatch, kind, failure_stage):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path)); monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('CONTABOT_CLIENTES_ROOT', str(tmp_path/'clients'))
    monkeypatch.setattr(fiscal_query_flow, '_WORKFLOW_MENU_OUTPUT_DIR', str(tmp_path/'output'))
    skill = 'ccma-obligaciones-pagos' if kind == 'ccma' else 'sct-estado-cumplimiento'
    scripts = tmp_path/'skills/productivity'/skill/'scripts'; scripts.mkdir(parents=True)
    (scripts/('arca_ccma_probe.js' if kind == 'ccma' else 'sct_probe.js')).touch()
    (scripts/'sct_xlsx.py').touch()
    error = credentials.FiscalDatabaseError('fiscal_database_permissions')
    database = AsyncMock(side_effect=error if failure_stage == 'preflight' else None)
    monkeypatch.setattr(fiscal_runtime, 'require_fiscal_database', database)
    runtime = AsyncMock(return_value=True)
    monkeypatch.setattr(fiscal_runtime, '_check', runtime)
    module = ccma_dispatch if kind == 'ccma' else fiscal_query_flow
    access = AsyncMock(side_effect=error)
    monkeypatch.setattr(module, 'canonical_access', access)
    child = AsyncMock(side_effect=AssertionError('must not launch ARCA'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', child)
    flow = fiscal_query_flow.FiscalQueryFlow(catalog=NS(runtime_python=sys.executable))
    flow.send = AsyncMock(); flow.send_document = AsyncMock()
    args = dict(chat_id='7',state_key=('7','8'),contributor_id=8,holder_cuit='20123456783',
                client_slug='client',client_cuit='20987654321',credential_line=None,credential_sha256=None)
    if kind == 'ccma':
        await ccma_dispatch.run_ccma(flow, **args, period_from='01/2026', period_to='01/2026')
    else:
        await flow._run_sct_dispatch(**args, period_mode='range', period_from='20260100',
                                     period_until='20260131', period_label='01/2026')
    database.assert_awaited_once()
    if failure_stage == 'preflight':
        access.assert_not_awaited(); runtime.assert_not_awaited()
    else:
        access.assert_awaited_once()
    child.assert_not_awaited(); flow.send_document.assert_not_awaited()
    text = flow.send.await_args.args[1]
    assert 'permisos' in text and 'No se consultó ARCA' in text
    assert 'Iniciá' not in text and 'CAPTCHA' not in text
