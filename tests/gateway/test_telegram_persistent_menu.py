import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from plugins.platforms.telegram.adapter import TelegramAdapter


def test_rich_reply_markup_is_one_persistent_menu_trigger():
    assert TelegramAdapter._persistent_menu_reply_markup() == {
        "keyboard": [[{"text": "☰ Menú"}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def test_menu_avatar_sticker_is_a_transparent_circle():
    path = TelegramAdapter._menu_avatar_sticker_path()

    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        assert image.format == "WEBP"
        assert rgba.size == (512, 512)
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((511, 511))[3] == 0
        assert rgba.getchannel("A").getbbox() == (32, 32, 480, 480)


def test_menu_trigger_sends_the_inline_panel_without_dispatching_an_agent_turn(monkeypatch):
    async def scenario():
        import plugins.platforms.telegram.adapter as adapter_module

        adapter = object.__new__(TelegramAdapter)
        adapter._is_user_authorized_from_message = lambda message: True
        adapter._agip_ddjj_flow = SimpleNamespace(text=AsyncMock(return_value=False))
        adapter._should_process_message = lambda message: False
        adapter._should_observe_unmentioned_group_message = lambda message: False

        monkeypatch.setattr(
            adapter_module,
            "InlineKeyboardButton",
            lambda text, callback_data: {"text": text, "callback_data": callback_data},
        )
        monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

        message = SimpleNamespace(
            text="☰ Menú",
            reply_sticker=AsyncMock(return_value=SimpleNamespace(sticker=None)),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(effective_message=message, update_id=1)

        await adapter._handle_text_message(update, SimpleNamespace(bot=SimpleNamespace()))

        message.reply_sticker.assert_awaited_once_with(
            sticker=TelegramAdapter._menu_avatar_sticker_path(),
        )
        message.reply_text.assert_awaited_once_with(
            "¿Qué querés hacer?",
            reply_markup=[
                [
                    {"text": "Consultar", "callback_data": "om:consultar"},
                    {"text": "Preparar / generar", "callback_data": "om:preparar"},
                ],
                [
                    {"text": "Conciliar", "callback_data": "om:conciliar"},
                    {"text": "Controlar / analizar", "callback_data": "om:controlar"},
                ],
                [{"text": "Ayuda", "callback_data": "om:ayuda"}],
            ],
        )
        adapter._agip_ddjj_flow.text.assert_not_awaited()

    asyncio.run(scenario())


def test_consult_menu_page_keeps_ddjj_as_the_only_functional_action(monkeypatch):
    async def scenario():
        import plugins.platforms.telegram.adapter as adapter_module

        adapter = object.__new__(TelegramAdapter)
        monkeypatch.setattr(
            adapter_module,
            "InlineKeyboardButton",
            lambda text, callback_data: {"text": text, "callback_data": callback_data},
        )
        monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

        query = SimpleNamespace(
            data="om:consultar",
            from_user=SimpleNamespace(first_name="Test"),
            message=SimpleNamespace(chat_id=1, chat=SimpleNamespace(type="private")),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await adapter._handle_callback_query(update, SimpleNamespace())

        query.answer.assert_awaited_once_with()
        query.edit_message_text.assert_awaited_once_with(
            "Consultas\n\nMaqueta: sólo DDJJ IIBB ejecuta una acción.",
            reply_markup=[
                [{"text": "Consultar DDJJ IIBB", "callback_data": "ad:start"}],
                [{"text": "Vencimientos", "callback_data": "om:noop"}],
                [{"text": "Retenciones", "callback_data": "om:noop"}],
                [
                    {"text": "‹ Menú", "callback_data": "om:main"},
                    {"text": "Cerrar", "callback_data": "om:close"},
                ],
            ],
        )

    asyncio.run(scenario())


def test_photo_menu_navigation_edits_the_caption(monkeypatch):
    async def scenario():
        import plugins.platforms.telegram.adapter as adapter_module

        adapter = object.__new__(TelegramAdapter)
        monkeypatch.setattr(
            adapter_module,
            "InlineKeyboardButton",
            lambda text, callback_data: {"text": text, "callback_data": callback_data},
        )
        monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(photo=[SimpleNamespace(file_id="avatar")]),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_operational_menu_callback(query, "om:preparar")

        query.edit_message_caption.assert_awaited_once_with(
            caption="Preparar / generar",
            reply_markup=[
                [{"text": "Convertir PDF a Excel", "callback_data": "px:start"}],
                [{"text": "Procesar lote de resúmenes", "callback_data": "bx:start"}],
                [
                    {"text": "Proteger PDF", "callback_data": "ps:protect:start"},
                    {"text": "Desbloquear PDF", "callback_data": "ps:unlock:start"},
                ],
                [{"text": "Liquidaciones", "callback_data": "om:noop"}],
                [{"text": "Papeles de trabajo", "callback_data": "om:noop"}],
                [
                    {"text": "‹ Menú", "callback_data": "om:main"},
                    {"text": "Cerrar", "callback_data": "om:close"},
                ],
            ],
        )
        query.edit_message_text.assert_not_awaited()

    asyncio.run(scenario())
