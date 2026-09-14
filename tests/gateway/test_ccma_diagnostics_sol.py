import json
import hashlib
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from plugins.platforms.telegram.ccma_diagnostics import Diagnostics, CODES
from plugins.platforms.telegram.ccma_dispatch import run_ccma


def test_projection_never_stores_unknown_strings_or_raw_exceptions(tmp_path):
    diag = Diagnostics(tmp_path)
    diag.record({'stage': 'table', 'code': ['secret'], 'error_kind': 'secret', 'password': 'secret',
                 'tables': [{'rows': 4, 'headings': [['detalle', 'secret', '20123456789']], 'raw': 'secret'}]})
    diag.record({'stage': {}, 'tables': [{'headings': 'secret'}]})
    for code in CODES:
        assert code in diag.failure(code, error=ValueError('secret'))
    diag.close()
    raw = (tmp_path / 'diagnostic.jsonl').read_text()
    assert 'secret' not in raw and '20123456789' not in raw
    assert (tmp_path / 'diagnostic.jsonl').stat().st_mode & 0o777 == 0o600


def test_observed_subcpto_heading_survives_projection_without_private_text(tmp_path):
    diag = Diagnostics(tmp_path)
    diag.record({'stage': 'table', 'tables': [{'headings': [['subcpto', 'SECRET']]}]})
    diag.close()
    row = json.loads((tmp_path / 'diagnostic.jsonl').read_text())
    assert row['tables'][0]['headings'] == [['subcpto', '?']]


def test_screenshot_metadata_and_rejection_message(tmp_path):
    diag = Diagnostics(tmp_path)
    diag.record({'stage': 'login', 'code': 'failure_screenshot_saved',
                 'screenshot': {'file': 'failure.png', 'bytes': 123, 'sha256': 'a'*64, 'password': 'SECRET'}})
    message=diag.failure('login_credentials_rejected')
    diag.close()
    rows=[json.loads(x) for x in (tmp_path/'diagnostic.jsonl').read_text().splitlines()]
    assert rows[0]['screenshot']=={'file':'failure.png','bytes':123,'sha256':'a'*64}
    assert 'Clave o usuario incorrecto' in message and 'SECRET' not in str(rows)


@pytest.mark.parametrize('code, explanation', [
    ('service_catalog_load_timeout', 'ARCA no terminó de cargar el catálogo'),
    ('service_open_timeout', 'CCMA no terminó de abrir'),
    ('ccma_account_load_timeout', 'CCMA no terminó de cargar la cuenta'),
    ('ccma_entry_ambiguous', 'más de una entrada visible'),
])
def test_navigation_failure_is_not_misreported_as_wrong_identity(tmp_path, code, explanation):
    diag = Diagnostics(tmp_path)
    diag.record({'stage': 'service' if code != 'ccma_account_load_timeout' else 'subject'})
    message = diag.failure(code)
    diag.close()
    assert explanation in message
    assert 'no se pudo identificar de forma única' not in message
    assert code in (tmp_path / 'diagnostic.jsonl').read_text()


def test_shared_login_and_service_events_survive_projection(tmp_path):
    diag = Diagnostics(tmp_path)
    diag.record({'stage': 'login', 'code': 'login_verified'})
    diag.record({'stage': 'service', 'code': 'service_opened'})
    diag.record({'stage': 'service', 'code': 'runner_error', 'error_kind': 'TimeoutException', 'message': 'SECRET'})
    diag.close()
    raw = (tmp_path / 'diagnostic.jsonl').read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert [row['code'] for row in rows] == ['login_verified', 'service_opened', 'runner_error']
    assert rows[-1]['error_kind'] == 'TimeoutException'
    assert 'SECRET' not in raw


@pytest.mark.parametrize('kind', ['JavascriptException', 'NoSuchWindowException', 'NoSuchFrameException',
    'InvalidSessionIdException', 'UnexpectedAlertPresentException', 'WebDriverException',
    'ElementClickInterceptedException', 'ElementNotInteractableException', 'InvalidSelectorException'])
def test_selenium_exception_class_and_location_survive_without_exception_text(tmp_path, kind):
    diag = Diagnostics(tmp_path)
    diag.record({'stage':'service', 'code':'runner_exception', 'error_kind':kind,
                 'error_at':'arca_services.py:ready:87', 'message':'SECRET'})
    diag.record({'error_at':'SECRET/../../private:bad:1'})
    diag.close()
    raw = (tmp_path/'diagnostic.jsonl').read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows[0]['error_kind'] == kind
    assert rows[0]['error_at'] == 'arca_services.py:ready:87'
    assert 'error_at' not in rows[1]
    assert 'SECRET' not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['subject_not_verified', 'login_not_verified', 'source_table_ambiguous', 'source_table_evaluation_failed'])
async def test_real_process_preserves_stage_code_and_reference(tmp_path, monkeypatch, status):
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / '.hermes'
    monkeypatch.setenv('HERMES_HOME', str(home))
    scripts = home / 'skills/productivity/ccma-obligaciones-pagos/scripts'
    scripts.mkdir(parents=True)
    credential = home / '.arca.csv'
    credential.write_text('synthetic'); credential.chmod(0o600)
    (scripts / 'arca_ccma_probe.js').write_text(
        "if(process.argv.includes('--preflight')){console.log('result=fiscal_runtime_ready');process.exit(0);}"
        "process.stderr.write('FISCAL_DIAGNOSTIC:'+JSON.stringify({stage:'table',tables:[{headings:[['detalle','SECRET']],rows:1}]})+'\\n');"
        "console.error('SECRET raw browser exception');console.log('result=" + status + "');")
    flow = NS(catalog=NS(runtime_python=sys.executable), send=AsyncMock(), send_document=AsyncMock(),
              _sct_dispatch_processes={}, _sct_runner_status=lambda b: b.decode().strip().split('=', 1)[1])
    await run_ccma(flow, chat_id='test', state_key=('test','test'), credential_line=2,
                   credential_sha256=hashlib.sha256(credential.read_bytes()).hexdigest(),
                   period_from='01/2025', period_to='12/2025', client_slug='synthetic', client_cuit='00000000000')
    files = list(tmp_path.glob('hermes-workspace/output/private/ccma-runs/*/diagnostic.jsonl'))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[-1]['stage'] == 'table' and rows[-1]['code'] == status
    assert 'SECRET' not in files[0].read_text()
    assert status in flow.send.await_args.args[1] and files[0].parent.name in flow.send.await_args.args[1]
    flow.send_document.assert_not_awaited()
