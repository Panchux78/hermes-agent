"""Contrato Telegram de #132, sin Telegram ni ARCA real."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import load_workbook

from plugins.platforms.telegram.constancias_flow import ConstanciasFlow, State
from plugins.platforms.telegram.contributor_selector import ContributorOffer
from plugins.platforms.telegram import contributor_selector


def test_search_keeps_study_binding_and_standard_chooser():
    rows = [
        {"id": 1, "nombre": "Ejemplo A", "slug": "ejemplo-a", "cuit": "20123456789", "study_id": 7},
        {"id": 2, "nombre": "Ejemplo B", "slug": "ejemplo-b", "cuit": "20987654321", "study_id": 8},
    ]
    with patch.object(ConstanciasFlow, "_query", return_value=rows) as query:
        found = ConstanciasFlow._search(12345, "Ejemplo")
    assert found == rows
    assert "fn_constancia_telegram_buscar" in query.call_args.args[0]
    assert ContributorOffer.from_rows(found).status == "multiple"
    with patch.object(contributor_selector, "InlineKeyboardMarkup", lambda items: items), \
         patch.object(contributor_selector, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data}):
        keyboard = ConstanciasFlow._candidate_keyboard(found, "abc")
    assert len(keyboard) == 3
    assert keyboard[0][0]["callback_data"] == "ci:select:abc:1"


def test_db_commands_bind_actor_study_and_job():
    with patch.object(ConstanciasFlow, "_query", return_value=[{"id_lote": 44}]) as query:
        assert ConstanciasFlow._begin(12345, 7, 9) == 44
    assert "fn_constancia_telegram_iniciar_confirmado(12345,7,9,NULL)" in query.call_args.args[0]
    with patch.object(ConstanciasFlow, "_query", return_value=[{"id_lote": 45}]) as query:
        assert ConstanciasFlow._begin(12345, 7, None, "10") == 45
    assert "fn_constancia_telegram_iniciar_confirmado(12345,7,NULL," in query.call_args.args[0]
    with patch.object(ConstanciasFlow, "_query", return_value=[{"estado": "terminada"}]) as query:
        assert ConstanciasFlow._status(12345, 44)["estado"] == "terminada"
    assert "fn_constancia_telegram_estado(12345,44)" in query.call_args.args[0]


def test_progress_and_terminal_summary_are_factual():
    status = {"estado": "consultando", "respondidos": 3, "solicitados": 12}
    assert "3/12" in ConstanciasFlow._summary(status, False)
    status.update(estado="terminada", responded=12, respondidos=12,
                  ri=5, monotributo=4, revisar=1, sin_verificar=2)
    summary = ConstanciasFlow._summary(status, False)
    assert "Responsable inscripto: 5" in summary
    assert "Revisar: 1" in summary
    assert "Sin verificar: 2" in summary


def test_book_contains_same_normalized_condition_not_pdf():
    path = ConstanciasFlow._book([
        {"nombre": "Ejemplo", "slug": "ejemplo", "condicion": "ri", "periodo_estado": "202605",
         "estado": "verificado", "consultado_en": None},
        {"nombre": "Otro", "slug": "otro", "condicion": None, "periodo_estado": None,
         "estado": "error", "consultado_en": None},
        {"nombre": "Pendiente", "slug": "pendiente", "condicion": None, "periodo_estado": None,
         "estado": "pendiente", "consultado_en": None},
        {"nombre": "Doble", "slug": "doble", "condicion": None, "periodo_estado": None,
         "estado": "revisar", "consultado_en": None},
    ])
    try:
        assert Path(path).stat().st_mode & 0o077 == 0
        sheet = load_workbook(path, read_only=True).active
        assert sheet["C2"].value == "Responsable inscripto"
        assert sheet["D2"].value == "05/2026"
        assert sheet["C3"].value == "No se pudo verificar"
        assert sheet["C4"].value == "Todavía no se consultó"
        assert sheet["E4"].value == "Sin consultar"
        assert sheet["C5"].value == "Revisar: ARCA informó más de una condición activa"
    finally:
        path.unlink(missing_ok=True)


def test_portfolio_never_starts_without_exact_typed_count():
    flow = ConstanciasFlow()
    state = State("nonce", "12345", 0.0, stage="confirm", study_id=7, expected_count=12)
    flow.states[flow._key(55, None, 12345)] = state
    message = SimpleNamespace(chat_id=55, message_thread_id=None,
                              from_user=SimpleNamespace(id=12345),
                              text="11", reply_text=AsyncMock())
    async def run():
        with patch.object(flow, "_start", new_callable=AsyncMock) as start:
            assert await flow.text(None, message)
            start.assert_not_awaited()
            message.text = "12"
            assert await flow.text(None, message)
            start.assert_awaited_once()
    asyncio.run(run())
    assert state.confirmation == "12"
