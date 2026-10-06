"""SCT failure status survives a real child process exiting nonzero."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from plugins.platforms.telegram import fiscal_query_flow as module
from tests.gateway.documentos_falsos import instalar as instalar_documentos

@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['login_credentials_rejected', 'runner_error', 'sct_subject_mismatch', 'sct_subject_identity_missing', 'subject_selector_does_not_contain_target', 'sct_parameters_load_timeout', 'parameters_screen_not_verified', None, 'sct_exported'])
async def test_nonzero_child_reports_status_without_delivering(tmp_path, monkeypatch, status):
    scripts=tmp_path/'profile/skills/productivity/sct-estado-cumplimiento/scripts'
    scripts.mkdir(parents=True)
    output='' if status is None else 'result='+status+'\n'
    (scripts/'sct_probe.js').write_text("if(process.argv[2]==='--preflight'){console.log('result=fiscal_runtime_ready');process.exit(0);}process.stdin.once('data',()=>{process.stdout.write("+json.dumps(output)+");process.exitCode=1;process.stdin.destroy();});")
    (scripts/'sct_xlsx.py').touch()
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('HERMES_HOME',str(tmp_path/'profile'))
    monkeypatch.setenv('CONTABOT_CLIENTES_ROOT',str(tmp_path/'clients'))
    instalar_documentos(monkeypatch, tmp_path)
    monkeypatch.setattr(module,'_WORKFLOW_MENU_OUTPUT_DIR',str(tmp_path/'output'))
    monkeypatch.setattr('plugins.platforms.telegram.fiscal_runtime.require_fiscal_database',AsyncMock())
    monkeypatch.setattr(module,'canonical_access',AsyncMock(return_value=b'{"password":"private-synthetic"}\n'))
    flow=module.FiscalQueryFlow(catalog=NS(runtime_python=sys.executable));flow.send=AsyncMock();flow.send_document=AsyncMock()
    await flow._run_sct_dispatch(chat_id='1',state_key=('1','1'),credential_line=None,credential_sha256=None,contributor_id=1,holder_cuit='20123456783',client_cuit='20987654321',client_slug='synthetic',period_mode='range',period_from='20250100',period_until='20251231',period_label='2025')
    text=' '.join(call.args[1] for call in flow.send.await_args_list)
    if status is None: assert 'sin un estado verificable' in text
    elif status=='sct_exported': assert 'informó exportación, pero terminó con error' in text
    else:
        expected = {
            'login_credentials_rejected': 'ARCA rechazó el usuario o la clave',
            'runner_error': 'error técnico durante la consulta',
            'sct_subject_mismatch': 'no coincide con el contribuyente solicitado',
            'sct_subject_identity_missing': 'CUIT verificable en Información del usuario',
            'subject_selector_does_not_contain_target': 'no figura entre los representados',
            'sct_parameters_load_timeout': 'no terminó de cargar el formulario de Estado de cumplimiento',
            'parameters_screen_not_verified': 'formulario de Estado de cumplimiento ambiguo',
        }[status]
        assert expected in text and status not in text
    assert 'Referencia:' in text and 'private-synthetic' not in text
    flow.send_document.assert_not_awaited()
