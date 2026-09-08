import re

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.admin_maintenance_flow import AdminMaintenanceFlow
from plugins.platforms.telegram.agip_ddjj_flow import AgipDdjjFlow
from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow
from plugins.platforms.telegram.menu_buttons import (
    BRAILLE_PATTERN_BLANK,
    HAIR_SPACE,
    MENU_ALIGNMENT_PADDING,
    aligned_menu_label,
    menu_label,
)
from plugins.platforms.telegram.pdf_security_flow import PdfSecurityFlow
from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow


_PRESENTATION_ONLY = {"\u200a", "\u2800", "\ufe0f"}
_ENGLISH_CONTROL = re.compile(
    r"\b(Yes|No|Allow|Deny|Cancel|Prev|Next|Back|Once|Session|Always)\b"
)


def _as_dict_markup(monkeypatch, module):
    monkeypatch.setattr(
        module,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
        raising=False,
    )
    monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: rows, raising=False)


def _rows(markup):
    if isinstance(markup, list):
        return markup
    return [
        [{"text": button.text, "callback_data": button.callback_data} for button in row]
        for row in markup.inline_keyboard
    ]


def _assert_safe_callbacks(rows):
    for row in rows:
        for button in row:
            callback = button["callback_data"]
            assert not any(character in callback for character in _PRESENTATION_ONLY)
            assert callback.isascii()


def _assert_contract_labels(rows):
    for row in rows:
        for button in row:
            text = button["text"]
            assert re.fullmatch(r"\S+  \S.*", text)
            assert _ENGLISH_CONTROL.search(text) is None


def test_menu_label_uses_exactly_two_ascii_spaces():
    assert menu_label("🔎", "Consultar") == "🔎  Consultar"
    assert menu_label("‹", "Menú") == "‹  Menú"
    with pytest.raises(ValueError):
        menu_label("", "Consultar")
    with pytest.raises(ValueError):
        menu_label("🔎", "")
    with pytest.raises(ValueError):
        menu_label("🔎 ", "Consultar")
    with pytest.raises(ValueError):
        menu_label("🔎", " Consultar")


def test_aligned_menu_label_only_appends_declared_presentation_characters():
    assert aligned_menu_label("arca", "🧾", "Preparar") == (
        "🧾  Preparar" + HAIR_SPACE * 2
    )
    assert aligned_menu_label("unknown", "🔎", "Consultar") == "🔎  Consultar"
    for (page, text), (braille, hair) in MENU_ALIGNMENT_PADDING.items():
        label = aligned_menu_label(page, "X", text)
        assert label == (
            menu_label("X", text)
            + HAIR_SPACE * hair
            + BRAILLE_PATTERN_BLANK * braille
        )


def test_operational_submenus_stack_actions_and_share_only_navigation(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    _as_dict_markup(monkeypatch, module)
    pages = (
        "organismos",
        "arca",
        "agip",
        "arba",
        "arca_consultar",
        "arca_preparar",
        "agip_consultar",
        "bancos",
        "herramientas",
        "ayuda",
        "administracion",
    )
    for page in pages:
        rows = TelegramAdapter._menu_panel_keyboard(page, show_administration=True)
        assert all(len(row) == 1 for row in rows[:-1])
        assert [button["text"] for button in rows[-1]] == [
            rows[-1][0]["text"],
            "✕  Cerrar",
        ]
        assert rows[-1][0]["text"].startswith("‹  ")
        _assert_contract_labels(rows)
        _assert_safe_callbacks(rows)


def test_alignment_padding_is_restricted_to_declared_fixed_buttons(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    _as_dict_markup(monkeypatch, module)
    main = TelegramAdapter._menu_panel_keyboard("main", show_administration=True)
    assert all(len(row) == 1 for row in main)
    _assert_contract_labels(main)
    assert all("\u200a" in row[0]["text"] or "\u2800" in row[0]["text"] for row in main)

    fixed_keyboards = {
        page: _rows(TelegramAdapter._menu_panel_keyboard(page, show_administration=True))
        for page in (
            "main", "organismos", "arca", "agip", "arba", "bancos",
            "herramientas", "ayuda", "administracion",
        )
    }
    fixed_keyboards.update({
        "pdf_consentimiento": _rows(PdfSecurityFlow._unlock_consent_keyboard("n")),
        "admin_confirmacion": _rows(AdminMaintenanceFlow._confirmation_keyboard("bcra", "n")),
        "lote_confirmacion": _rows(BatchPdfXlsxFlow._confirmation_keyboard("batch")),
    })

    used = set()
    for page, rows in fixed_keyboards.items():
        for row in rows:
            for button in row:
                label = button["text"]
                visible = label.rstrip(HAIR_SPACE + BRAILLE_PATTERN_BLANK)
                icon, text = visible.split("  ", 1)
                has_padding = visible != label
                if has_padding:
                    assert (page, text) in MENU_ALIGNMENT_PADDING
                    assert label == aligned_menu_label(page, icon, text)
                    used.add((page, text))
                if button["callback_data"].startswith("om:") and text in {
                    "Menú", "Organismos", "Cerrar",
                }:
                    assert not has_padding

    flow_pages = {"pdf_consentimiento", "admin_confirmacion", "lote_confirmacion"}
    assert used == {
        key for key in MENU_ALIGNMENT_PADDING if key[0] not in flow_pages
    }


def test_business_flow_keyboards_follow_icons_spacing_and_rows(monkeypatch):
    import telegram
    import plugins.platforms.telegram.admin_maintenance_flow as admin_module
    import plugins.platforms.telegram.agip_ddjj_flow as agip_module
    import plugins.platforms.telegram.batch_pdf_xlsx_flow as batch_module
    import plugins.platforms.telegram.pdf_security_flow as security_module
    import plugins.platforms.telegram.portal_iva_flow as portal_module

    monkeypatch.setattr(
        telegram,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )
    monkeypatch.setattr(telegram, "InlineKeyboardMarkup", lambda rows: rows)
    for module in (admin_module, agip_module, batch_module, security_module, portal_module):
        _as_dict_markup(monkeypatch, module)

    candidates = [{"id": 7, "nombre": "Empresa", "cuit": "30-12345678-9"}]
    keyboards = tuple(_rows(markup) for markup in (
        PdfSecurityFlow._unlock_consent_keyboard("n"),
        AdminMaintenanceFlow._confirmation_keyboard("bcra", "n"),
        AdminMaintenanceFlow._cancel_run_keyboard(),
        BatchPdfXlsxFlow._confirmation_keyboard("batch"),
        AgipDdjjFlow._cancel_keyboard("n"),
        AgipDdjjFlow._candidate_keyboard(candidates, "n"),
        PortalIvaFlow._cancel_keyboard("n"),
        PortalIvaFlow._candidate_keyboard(candidates, "n"),
    ))
    for rows in keyboards:
        assert all(len(row) == 1 for row in rows)
        _assert_contract_labels(rows)
        _assert_safe_callbacks(rows)

    assert [row[0]["text"] for row in keyboards[0]] == [
        "✅  Acepto y continuar",
        "❌  Cancelar" + BRAILLE_PATTERN_BLANK * 7,
    ]
    assert [row[0]["text"] for row in keyboards[1]] == [
        "✅  Confirmar actualización",
        "❌  Cancelar" + HAIR_SPACE + BRAILLE_PATTERN_BLANK * 10,
    ]
    assert keyboards[3][0][0]["text"] == "✅  Procesar lote"
    assert keyboards[3][1][0]["text"] == "❌  Cancelar" + BRAILLE_PATTERN_BLANK * 3
    assert keyboards[5][0][0]["text"].startswith("👤  Empresa — ")
    assert keyboards[7][0][0]["text"].startswith("👤  Empresa — ")
