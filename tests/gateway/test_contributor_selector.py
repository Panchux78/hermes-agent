from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugins.platforms.telegram.contributor_selector import (
    ContributorOffer,
    MULTIPLE_CONTRIBUTORS_TEXT,
)


ROWS = [
    {"id": 7, "nombre": "Berenstein Ariel", "cuit": "20123456786", "slug": "berenstein-ariel"},
    {"id": 8, "nombre": "Berenstein Jorge", "cuit": "20222222223", "slug": "berenstein-jorge"},
]


def test_offer_resolves_zero_one_multiple_and_preserves_full_payload():
    assert ContributorOffer.from_rows([]).status == "empty"
    one = ContributorOffer.from_rows([ROWS[0]])
    assert one.status == "single"
    assert one.single == ROWS[0]
    multiple = ContributorOffer.from_rows(ROWS)
    assert multiple.status == "multiple"
    assert multiple.candidate_ids == (7, 8)
    assert multiple.pick(8) == ROWS[1]
    assert multiple.pick(999) is None


def test_offer_rejects_malformed_or_duplicate_candidates():
    with pytest.raises(ValueError, match="contributor_candidate_invalid"):
        ContributorOffer.from_rows([{"id": 1, "nombre": "Sin identidad"}])
    with pytest.raises(ValueError, match="contributor_candidate_duplicate"):
        ContributorOffer.from_rows([ROWS[0], dict(ROWS[0])])


def test_shared_keyboard_disambiguates_homonyms_and_keeps_cancel_last():
    import plugins.platforms.telegram.contributor_selector as module

    with (
        patch.object(module, "InlineKeyboardButton", side_effect=lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data)),
        patch.object(module, "InlineKeyboardMarkup", side_effect=lambda rows: SimpleNamespace(inline_keyboard=rows)),
    ):
        keyboard = ContributorOffer.from_rows(ROWS).keyboard(
            callback_prefix="ve", nonce="nonce", cancel_text="❌  Cancelar"
        )
    assert MULTIPLE_CONTRIBUTORS_TEXT == "Encontré varias coincidencias. Elegí un contribuyente:"
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        "ve:select:nonce:7",
        "ve:select:nonce:8",
        "ve:cancel:nonce",
    ]
    assert keyboard.inline_keyboard[0][0].text != keyboard.inline_keyboard[1][0].text
    assert "20-******-6" in keyboard.inline_keyboard[0][0].text


def test_all_current_tax_workflows_use_the_same_picker_contract(monkeypatch):
    import plugins.platforms.telegram.agip_ddjj_flow as agip_module
    import plugins.platforms.telegram.fiscal_query_flow as fiscal_module
    import plugins.platforms.telegram.portal_iva_flow as portal_module
    import plugins.platforms.telegram.vencimientos_flow as due_module
    from plugins.platforms.telegram.agip_ddjj_flow import AgipDdjjFlow
    from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow, _WorkflowMenuState
    from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow
    from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow

    for module in (agip_module, fiscal_module, portal_module, due_module):
        monkeypatch.setattr(module, "InlineKeyboardButton", lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
        monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: SimpleNamespace(inline_keyboard=rows))

    fiscal = FiscalQueryFlow(catalog=SimpleNamespace())
    state = _WorkflowMenuState(skill_command="ccma", label="CCMA", nonce="nonce")
    keyboards = {
        "fq": fiscal._candidate_keyboard(state, ROWS),
        "pi": PortalIvaFlow._candidate_keyboard(ROWS, "nonce"),
        "ad": AgipDdjjFlow._candidate_keyboard(ROWS, "nonce"),
        "ve": VencimientosFlow._candidate_keyboard(ROWS, "nonce"),
    }
    for prefix, keyboard in keyboards.items():
        assert [row[0].text for row in keyboard.inline_keyboard] == [
            "👤  Berenstein Ariel — 20-******-6",
            "👤  Berenstein Jorge — 20-******-3",
            "❌  Cancelar",
        ]
        assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
            f"{prefix}:select:nonce:7",
            f"{prefix}:select:nonce:8",
            f"{prefix}:cancel:nonce",
        ]
