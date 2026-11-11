"""Ágora 159: TLS failures reach Telegram without becoming a timeout."""
import json
from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()
from plugins.platforms.telegram.ccma_diagnostics import Diagnostics
from plugins.platforms.telegram.fiscal_query_flow import sct_failure_message


def test_ccma_preserves_certificate_reason_in_message_and_diagnostic(tmp_path):
    diagnostic = Diagnostics(tmp_path)
    try:
        message = diagnostic.failure('arca_certificado_no_verificable')
    finally:
        diagnostic.close()
    assert 'certificado que no se pudo verificar' in message
    assert 'no se ingresó y no se reintentó' in message
    rows = [json.loads(line) for line in (tmp_path/'diagnostic.jsonl').read_text().splitlines()]
    assert rows[-1]['code'] == 'arca_certificado_no_verificable'


def test_sct_explains_certificate_failure_and_preserves_reference():
    message = sct_failure_message('arca_certificado_no_verificable', 'synthetic-reference')
    assert 'certificado que no se pudo verificar' in message
    assert 'no se ingresó y no se reintentó' in message
    assert 'Referencia: synthetic-reference' in message
    assert 'error técnico' not in message
