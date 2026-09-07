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
        adapter._portal_iva_flow = SimpleNamespace(text=AsyncMock(return_value=False))
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
            "Hola, soy ContaBot, tu asistente de IA contable.\n"
            "¿Qué querés hacer hoy?",
            reply_markup=[
                [
                    {
                        "text": "🏛️ Organismos fiscales" + "\u200a" * 3 + "\u2800" * 3,
                        "callback_data": "om:organismos",
                    },
                ],
                [
                    {
                        "text": "🏦 Bancos" + "\u200a" * 3 + "\u2800" * 12,
                        "callback_data": "om:bancos",
                    },
                ],
                [
                    {
                        "text": "🧰 Herramientas" + "\u200a" * 4 + "\u2800" * 7,
                        "callback_data": "om:herramientas",
                    },
                ],
                [
                    {
                        "text": "❓ " + "\u200a" * 2 + "Ayuda" + "\u2800" * 14,
                        "callback_data": "om:ayuda",
                    },
                ],
            ],
        )
        adapter._agip_ddjj_flow.text.assert_not_awaited()
        adapter._portal_iva_flow.text.assert_not_awaited()

    asyncio.run(scenario())


def test_organisms_menu_separates_tax_authorities(monkeypatch):
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
            data="om:organismos",
            from_user=SimpleNamespace(first_name="Test"),
            message=SimpleNamespace(chat_id=1, chat=SimpleNamespace(type="private")),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query)

        await adapter._handle_callback_query(update, SimpleNamespace())

        query.answer.assert_awaited_once_with()
        query.edit_message_text.assert_awaited_once_with(
            "Organismos fiscales\n\nElegí el organismo con el que necesitás operar.",
            reply_markup=[
                [
                    {"text": "ARCA", "callback_data": "om:arca"},
                    {"text": "AGIP", "callback_data": "om:agip"},
                ],
                [{"text": "ARBA · Próximamente", "callback_data": "om:arba"}],
                [
                    {"text": "‹ Menú", "callback_data": "om:main"},
                    {"text": "✕ Cerrar", "callback_data": "om:close"},
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
        monkeypatch.setattr(adapter_module.PortalIvaFlow, "available", lambda self: True)
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=SimpleNamespace(photo=[SimpleNamespace(file_id="avatar")]),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

        await adapter._handle_operational_menu_callback(query, "om:bancos")

        query.edit_message_caption.assert_awaited_once_with(
            caption=(
                "Bancos\n\n"
                "Convertí resúmenes bancarios compatibles a Excel. "
                "No convierte PDFs generales."
            ),
            reply_markup=[
                [{"text": "🏦 Resumen bancario → Excel", "callback_data": "px:start"}],
                [
                    {
                        "text": "📦 Lote de resúmenes bancarios → Excel",
                        "callback_data": "bx:start",
                    }
                ],
                [
                    {"text": "‹ Menú", "callback_data": "om:main"},
                    {"text": "✕ Cerrar", "callback_data": "om:close"},
                ],
            ],
        )
        query.edit_message_text.assert_not_awaited()

    asyncio.run(scenario())


def test_each_tax_authority_separates_query_prepare_and_present(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

    for authority in ("arca", "agip", "arba"):
        assert TelegramAdapter._menu_panel_keyboard(authority) == [
            [
                {
                    "text": "🔎 Consultar",
                    "callback_data": f"om:{authority}_consultar",
                },
                {
                    "text": "🧾 Preparar",
                    "callback_data": f"om:{authority}_preparar",
                },
            ],
            [
                {
                    "text": "📤 Presentar",
                    "callback_data": f"om:{authority}_presentar",
                }
            ],
            [
                {"text": "‹ Organismos", "callback_data": "om:organismos"},
                {"text": "✕ Cerrar", "callback_data": "om:close"},
            ],
        ]


def test_implemented_tax_actions_reuse_existing_flows(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    monkeypatch.setattr(
        adapter_module,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

    assert TelegramAdapter._menu_panel_keyboard("arca_consultar")[0] == [
        {"text": "📥 CSV de períodos presentados", "callback_data": "pi:descargar"}
    ]
    assert TelegramAdapter._menu_panel_keyboard("arca_preparar")[0] == [
        {"text": "🧾 Preparar período nuevo", "callback_data": "pi:generar"}
    ]
    assert TelegramAdapter._menu_panel_keyboard("agip_consultar")[0] == [
        {"text": "🧾 DDJJ de IIBB", "callback_data": "ad:start"}
    ]

    for page in (
        "arca_presentar",
        "agip_preparar",
        "agip_presentar",
        "arba_consultar",
        "arba_preparar",
        "arba_presentar",
    ):
        assert all(
            button["callback_data"].startswith("om:")
            for row in TelegramAdapter._menu_panel_keyboard(page)
            for button in row
        )


def test_pdf_tools_are_separate_from_accounting_preparation(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    monkeypatch.setattr(
        adapter_module,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

    tools = TelegramAdapter._menu_panel_keyboard("herramientas")
    banks = TelegramAdapter._menu_panel_keyboard("bancos")

    assert tools == [
        [{"text": "🔒 Proteger PDF", "callback_data": "ps:protect:start"}],
        [{"text": "🔓 Desbloquear PDF", "callback_data": "ps:unlock:start"}],
        [
            {"text": "‹ Menú", "callback_data": "om:main"},
            {"text": "✕ Cerrar", "callback_data": "om:close"},
        ],
    ]
    assert all(
        not button["callback_data"].startswith("ps:")
        for row in banks
        for button in row
    )


def test_operational_menu_has_no_placeholder_actions(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    monkeypatch.setattr(
        adapter_module,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

    for page in (
        "main",
        "organismos",
        "arca",
        "agip",
        "arba",
        "arca_consultar",
        "arca_preparar",
        "arca_presentar",
        "agip_consultar",
        "agip_preparar",
        "agip_presentar",
        "arba_consultar",
        "arba_preparar",
        "arba_presentar",
        "bancos",
        "herramientas",
        "ayuda",
    ):
        keyboard = TelegramAdapter._menu_panel_keyboard(page)
        assert all(
            button["callback_data"] != "om:noop"
            for row in keyboard
            for button in row
        )


def test_help_menu_offers_real_information_and_free_query_actions(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module

    monkeypatch.setattr(
        adapter_module,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)

    assert TelegramAdapter._menu_panel_keyboard("ayuda") == [
        [{"text": "ℹ️ Qué hace ContaBot", "callback_data": "om:que_hace"}],
        [{"text": "💬 Hacer una consulta", "callback_data": "om:consulta"}],
        [
            {"text": "‹ Menú", "callback_data": "om:main"},
            {"text": "✕ Cerrar", "callback_data": "om:close"},
        ],
    ]
    assert "Escribí tu consulta en el chat." in TelegramAdapter._menu_panel_title("consulta")
    assert "Convierte resúmenes bancarios" in TelegramAdapter._menu_panel_title("que_hace")


def test_portal_iva_callback_requires_existing_telegram_authorization():
    async def scenario():
        adapter = object.__new__(TelegramAdapter)
        adapter._is_callback_user_authorized = lambda *args, **kwargs: False
        adapter._portal_iva_flow = SimpleNamespace(callback=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="pi:start",
            from_user=SimpleNamespace(id="7", first_name="Test"),
            message=SimpleNamespace(chat_id="10", chat=SimpleNamespace(type="private"), message_thread_id=None),
            answer=AsyncMock(),
        )
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())
        query.answer.assert_awaited_once_with(text="⛔ No estás autorizado para consultar Portal IVA.")
        adapter._portal_iva_flow.callback.assert_not_awaited()

    asyncio.run(scenario())
