from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import os
import subprocess
import sys

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.document_delivery import delivered_document_filename
from plugins.platforms.telegram.operational_menu import OperationalMenu, PAGES, build_operational_menu


@pytest.fixture
def menu(tmp_path, monkeypatch):
    # gateway/conftest intentionally mocks the SDK. Keep a faithful value DTO
    # here so layout assertions exercise the complete page definitions.
    import plugins.platforms.telegram.operational_menu as module
    monkeypatch.setattr(module, "InlineKeyboardButton", lambda text, callback_data: NS(text=text, callback_data=callback_data))
    monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: NS(inline_keyboard=rows))
    return OperationalMenu({"operational_menu": {"name": "Lea"}, "technical_menu_user_id": "7",
        "contabot": {"project_dir": str(tmp_path / "project"), "runtime_python": str(tmp_path / "python"),
            "portal_iva_executor": str(tmp_path / "portal.py"), "arca_map": str(tmp_path / "map.json")}})


def query(data, uid="7", chat_id="7", chat_type="private"):
    return NS(data=data, from_user=NS(id=uid),
        message=NS(chat_id=chat_id, chat=NS(type=chat_type), message_thread_id=None),
        answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())


def context(q):
    return dict(chat_id=q.message.chat_id, chat_type=q.message.chat.type, thread_id=None, user_name=None)


def test_disabled_by_default_and_invalid_explicit_config_fails():
    assert build_operational_menu({}) is None
    with pytest.raises(ValueError, match="CONFIG_INVALID"):
        build_operational_menu({"operational_menu": True})
    with pytest.raises(ValueError, match="PATHS_REQUIRED"):
        build_operational_menu({"operational_menu": {"name": "Lea"}})


def test_all_pages_have_single_action_rows_and_back_to_actual_parent(menu):
    for page, (_, parent, _, _) in PAGES.items():
        rows = menu.keyboard(page, admin=True).inline_keyboard
        assert all(len(row) == 1 for row in rows[:-1])
        assert rows[-1][-1].callback_data == "om:close"
        assert len(rows[-1]) == (2 if parent else 1)
        if parent:
            assert rows[-1][0].callback_data == f"om:{parent}"
    assert menu.keyboard("que_hace").inline_keyboard[-1][0].callback_data == "om:ayuda"


def test_main_identity_icons_padding_and_distinct_portal_actions(menu):
    rows = menu.keyboard(admin=True).inline_keyboard
    assert [r[0].callback_data for r in rows[:-1]] == [
        "om:organismos", "om:bancos", "om:herramientas", "om:ayuda", "om:admin"]
    assert rows[0][0].text == "🏛️  Organismos fiscales" + "\u200a" * 3 + "\u2800" * 3
    assert [r[0].text for r in menu.keyboard("organismos").inline_keyboard[:-1]] == [
        "🏛️  ARCA", "💵  AGIP" + "\u200a" * 2, "🪙  ARBA"]
    assert menu.keyboard("arca_consultar").inline_keyboard[0][0].callback_data == "pi:descargar"
    assert menu.keyboard("arca_preparar").inline_keyboard[0][0].callback_data == "pi:generar"
    assert "Lea" in menu.keyboard("ayuda").inline_keyboard[0][0].text
    assert not any(b.callback_data == "om:admin" for r in menu.keyboard().inline_keyboard for b in r)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["om:main", "oa:bcra:start", "pi:generar", "ad:start", "px:start"])
async def test_denied_callback_never_enters_workflow(menu, data):
    adapter = NS(_callback_authorized=AsyncMock(return_value=False))
    for flow in menu.flows.values():
        flow.callback = AsyncMock()
    q = query(data)
    assert await menu.callback(adapter, q, context(q))
    for flow in menu.flows.values():
        flow.callback.assert_not_awaited()
    q.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("uid,chat,kind", [("8","8","private"), ("7","8","private"), ("7","7","group")])
async def test_admin_requires_exact_private_owner_even_with_allowlist(menu, uid, chat, kind):
    menu.flows["oa"].callback = AsyncMock()
    q = query("oa:bcra:start", uid, chat, kind)
    assert await menu.callback(NS(_callback_authorized=AsyncMock(return_value=True)), q, context(q))
    menu.flows["oa"].callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["px","bx","ps","pi","ad","oa"])
async def test_dispatches_existing_workflow_once(menu, prefix):
    flow = menu.flows[prefix]
    flow.callback = AsyncMock(return_value=True)
    if prefix == "pi":
        flow.available = Mock(return_value=True)
    adapter = NS(_callback_authorized=AsyncMock(return_value=True))
    q = query(prefix + ":start")
    assert await menu.callback(adapter, q, context(q))
    flow.callback.assert_awaited_once_with(adapter, q, q.data, "7", None, "7")


@pytest.mark.asyncio
async def test_close_is_navigation_not_operation_cancellation(menu):
    for flow in menu.flows.values():
        flow.callback = AsyncMock()
    q = query("om:close")
    await menu.callback(NS(_callback_authorized=AsyncMock(return_value=True)), q, context(q))
    q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    for flow in menu.flows.values():
        flow.callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_captcha_text_consumed_before_agent_and_unrelated_text_passes(menu):
    msg = NS(chat=NS(type="private"), text="ABC123")
    for flow in menu.flows.values():
        flow.text = AsyncMock(return_value=False)
    menu.flows["pi"].text.return_value = True
    assert await menu.text(NS(), msg)
    menu.flows["pi"].text.assert_awaited_once()
    menu.flows["pi"].text.return_value = False
    assert not await menu.text(NS(), msg)


@pytest.mark.asyncio
async def test_adapter_preserves_upstream_callback_and_normal_message(menu):
    adapter = TelegramAdapter(PlatformConfig(token="dummy"))
    adapter._operational_menu = menu
    adapter._handle_wisdom_agent_callback = AsyncMock()
    q = query("wa:existing")
    await adapter._handle_callback_query(NS(callback_query=q), None)
    adapter._handle_wisdom_agent_callback.assert_awaited_once_with(q, q.data)
    msg = NS(text="Consulta normal", chat=NS(type="private"))
    adapter._is_user_authorized_from_message = Mock(return_value=True)
    adapter._gate_or_observe = Mock(return_value=True)
    adapter._ensure_forum_commands = AsyncMock()
    event = NS(text=msg.text)
    adapter._build_triggered_event = AsyncMock(return_value=event)
    adapter._enqueue_text_event = Mock()
    for flow in menu.flows.values():
        flow.text = AsyncMock(return_value=False)
    await adapter._handle_text_message(NS(message=msg, update_id=1), None)
    adapter._enqueue_text_event.assert_called_once_with(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/start", "/model", "/status", "/skills"])
async def test_existing_commands_are_still_dispatched(menu, command):
    adapter = TelegramAdapter(PlatformConfig(token="dummy"))
    adapter._operational_menu = menu
    menu.open = AsyncMock()
    msg = NS(text=command, chat=NS(type="private"))
    adapter._should_process_message = Mock(return_value=True)
    adapter._is_user_authorized_from_message = Mock(return_value=True)
    adapter._ensure_forum_commands = AsyncMock()
    event = NS(text=command)
    adapter._build_triggered_event = AsyncMock(return_value=event)
    adapter.handle_message = AsyncMock()
    await adapter._handle_command(NS(message=msg, update_id=1), None)
    adapter.handle_message.assert_awaited_once_with(event)
    assert menu.open.await_count == (1 if command == "/start" else 0)


@pytest.mark.asyncio
async def test_document_transport_preserves_actual_returned_name(tmp_path):
    adapter = TelegramAdapter(PlatformConfig(token="dummy"))
    adapter._bot = NS(send_document=AsyncMock())
    adapter._send_media = AsyncMock(return_value=NS(message_id=42, document=NS(file_name="official.csv")))
    path = tmp_path / "official.csv"
    path.write_text("test")
    result = await adapter.send_document("7", str(path), file_name=path.name)
    assert result.success
    assert delivered_document_filename(result) == "official.csv"
    assert adapter._send_media.await_args.kwargs["filename"] == "official.csv"
    assert delivered_document_filename(SendResult(success=True)) is None


def test_menu_renders_with_real_installed_telegram_sdk(tmp_path):
    # Separate interpreter bypasses gateway/conftest's SDK mock. No credentials,
    # network, bot initialization, DB or process running a workflow.
    code = """
import json
from telegram import InlineKeyboardMarkup
from plugins.platforms.telegram.operational_menu import OperationalMenu, PAGES
m = object.__new__(OperationalMenu)
m.name = 'Lea'
for page, (_, parent, _, _) in PAGES.items():
    keyboard = m.keyboard(page, admin=True)
    assert isinstance(keyboard, InlineKeyboardMarkup)
    dto = keyboard.to_dict()['inline_keyboard']
    assert all(len(row) == 1 for row in dto[:-1])
    assert dto[-1][-1]['callback_data'] == 'om:close'
    assert len(dto[-1]) == (2 if parent else 1)
    json.dumps(dto)
print('REAL_SDK_MENU_OK')
"""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "HERMES_HOME": str(tmp_path / "hermes"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "REAL_SDK_MENU_OK"
