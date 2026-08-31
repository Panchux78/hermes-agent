import pytest

from plugins.platforms.telegram.agip_ddjj_flow import (
    is_valid_delivery_path,
    normalize_period,
    visible_cuit,
)


def test_normalize_period_accepts_year_month_and_date():
    assert normalize_period("2025") == "2025"
    assert normalize_period("202512") == "2025-12"
    assert normalize_period("2025-12") == "2025-12"
    assert normalize_period("31/12/2025") == "2025-12"


def test_normalize_period_rejects_invalid_values():
    with pytest.raises(ValueError):
        normalize_period("2025-13")
    with pytest.raises(ValueError):
        normalize_period("texto")


def test_visible_cuit_masks_middle_digits():
    assert visible_cuit("20123456789") == "20-******-9"


def test_delivery_accepts_the_v5_consultation_path_and_versions():
    assert is_valid_delivery_path(
        "/home/pancho/clientes/vgs-st-srl/30712345678/agip/2026/07/consultas/"
        "30712345678-ddjj-iibb-agip-2026-07.xlsx"
    )
    assert is_valid_delivery_path(
        "/home/pancho/clientes/vgs-st-srl/30712345678/agip/2026/anual/consultas/"
        "30712345678-ddjj-iibb-agip-2026-v02.xlsx"
    )
    assert not is_valid_delivery_path(
        "/home/pancho/clientes/vgs-st-srl/30712345678/agip/2026/2026-07/ddjj-vep/"
        "2026-08-14__ddjj-iibb-periodo-2026-07.xlsx"
    )
