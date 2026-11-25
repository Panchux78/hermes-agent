"""A failure in answerCallbackQuery is not a failure of the menu action."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.apocrifos_flow import ApocrifosFlow

errors = pytest.importorskip("telegram.error")
USER = 12345
TECHNICAL = "No pude completar esta acción por un error técnico"


def adapter_and_tap(data, answer_error):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), chat_id=USER,
                              reply_text=AsyncMock(), message_id=17, message_thread_id=None)
    query = SimpleNamespace(data=data, message=message, from_user=SimpleNamespace(id=USER),
                            answer=AsyncMock(side_effect=answer_error), edit_message_text=AsyncMock())
    return adapter, query


async def dispatch(adapter, query):
    update = SimpleNamespace(callback_query=query)
    try:
        await adapter._handle_callback_query(update, None)
    except Exception as exc:
        # Recorre el mismo manejo de error de PTB si el callback deja escapar la excepción.
        await adapter._handle_application_error(update, SimpleNamespace(error=exc))


@pytest.mark.asyncio
@pytest.mark.parametrize("error_cls", [errors.TimedOut, errors.NetworkError, TimeoutError])
async def test_network_error_in_ack_does_not_report_failure_or_stop_navigation(caplog, error_cls):
    adapter, query = adapter_and_tap("om:arca_consultar", error_cls("detalle sensible sintético"))
    with caplog.at_level(logging.WARNING):
        await dispatch(adapter, query)
    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    adapter._bot.send_message.assert_not_awaited()
    assert "answer_callback_query" in caplog.text
    assert "detalle sensible" not in caplog.text and "unhandled Telegram update" not in caplog.text


@pytest.mark.asyncio
async def test_error_in_the_action_still_reaches_the_normal_error_path(caplog):
    adapter, query = adapter_and_tap("om:arca_consultar", None)
    query.edit_message_text.side_effect = errors.TimedOut()
    await dispatch(adapter, query)
    assert TECHNICAL in adapter._bot.send_message.await_args.kwargs["text"]
    assert "unhandled Telegram update error type=TimedOut" in caplog.text


@pytest.mark.asyncio
async def test_apocrifos_asks_for_the_taxpayer_even_if_ack_times_out():
    adapter, query = adapter_and_tap("apo:uno", errors.TimedOut())
    flow = adapter._apocrifos_flow = ApocrifosFlow()
    scope = SimpleNamespace(require_actor=Mock(), require_permission=Mock())
    flow._scope = lambda: scope
    await dispatch(adapter, query)
    query.answer.assert_awaited_once()
    scope.require_permission.assert_called_once_with(str(USER), "operaciones.ejecutar")
    adapter._bot.send_message.assert_awaited_once()
    assert "Escribí nombre, CUIT o slug" in adapter._bot.send_message.await_args.kwargs["text"]
    assert next(iter(flow.states.values())).stage == "buscar"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_cls", [errors.BadRequest, ValueError])
async def test_programming_and_api_errors_in_ack_are_not_treated_as_failed_transport(error_cls):
    adapter, query = adapter_and_tap("om:arca_consultar", error_cls("respuesta no válida"))
    with pytest.raises(error_cls):
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_dispatch_is_not_revived():
    adapter, query = adapter_and_tap("om:arca_consultar", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
    query.edit_message_text.assert_not_awaited()
