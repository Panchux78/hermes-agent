import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.platforms.telegram.adapter import TelegramAdapter


def test_administration_button_only_appears_for_technical_user(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

    technical = TelegramAdapter._menu_panel_keyboard("main", show_administration=True)
    ordinary = TelegramAdapter._menu_panel_keyboard("main", show_administration=False)

    assert {button["text"] for row in technical for button in row} >= {"Administración"}
    assert "Administración" not in {button["text"] for row in ordinary for button in row}


def test_forged_administration_callback_is_rejected_without_flow(monkeypatch):
    async def scenario():
        adapter = object.__new__(TelegramAdapter)
        adapter.config = SimpleNamespace(extra={"technical_menu_user_id": "99"})
        adapter._admin_maintenance_flow = SimpleNamespace(callback=AsyncMock())
        query = SimpleNamespace(
            data="oa:arca:start",
            from_user=SimpleNamespace(id=100, first_name="Otro"),
            message=SimpleNamespace(chat_id=100, chat=SimpleNamespace(type="private"), message_thread_id=None),
            answer=AsyncMock(),
        )

        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

        query.answer.assert_awaited_once_with(text="⛔ No estás autorizado para administrar ContaBot.")
        adapter._admin_maintenance_flow.callback.assert_not_awaited()

    asyncio.run(scenario())


def test_technical_user_returns_to_main_menu_with_administration(monkeypatch):
    async def scenario():
        import plugins.platforms.telegram.adapter as adapter_module
        adapter = object.__new__(TelegramAdapter)
        adapter.config = SimpleNamespace(extra={"technical_menu_user_id": "99"})
        monkeypatch.setattr(adapter_module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
        monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)
        query = SimpleNamespace(
            data="om:main",
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(chat_id=99, chat=SimpleNamespace(type="private"), photo=None),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())
        keyboard = query.edit_message_text.call_args.kwargs["reply_markup"]
        assert "Administración" in {button["text"] for row in keyboard for button in row}
    asyncio.run(scenario())
