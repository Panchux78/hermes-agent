"""Shared visible-label contract for ContaBot Telegram buttons."""

MENU_SEPARATOR = "  "
HAIR_SPACE = "\u200a"
BRAILLE_PATTERN_BLANK = "\u2800"

# (page, visible text) -> (braille blanks, hair spaces).
# Telegram centers every label independently. These suffixes compensate fixed
# labels from the captured client evidence and remain subject to visual QA.
MENU_ALIGNMENT_PADDING = {
    ("main", "Organismos fiscales"): (3, 3),
    ("main", "Bancos"): (12, 3),
    ("main", "Herramientas"): (7, 4),
    ("main", "Ayuda"): (14, 0),
    ("main", "Administración"): (7, 1),
    ("organismos", "AGIP"): (0, 2),
    ("arca", "Preparar"): (0, 2),
    ("agip", "Preparar"): (0, 2),
    ("arba", "Preparar"): (0, 2),
    ("bancos", "Resumen bancario → Excel"): (7, 2),
    ("herramientas", "Proteger PDF"): (3, 0),
    ("ayuda", "Hacer una consulta"): (0, 1),
    ("administracion", "Actualizar bancos BCRA"): (4, 0),
    ("pdf_consentimiento", "Cancelar"): (7, 0),
    ("admin_confirmacion", "Cancelar"): (10, 1),
    ("lote_confirmacion", "Cancelar"): (3, 0),
}


def menu_label(icon: str, text: str) -> str:
    """Return an icon label separated by exactly two ASCII spaces."""
    if not icon or not text:
        raise ValueError("menu button icon and text are required")
    if icon != icon.strip() or text != text.strip():
        raise ValueError("menu button icon and text must not have edge whitespace")
    return f"{icon}{MENU_SEPARATOR}{text}"


def aligned_menu_label(page: str, icon: str, text: str) -> str:
    """Return a fixed menu label with its declared visual compensation."""
    braille_padding, hair_padding = MENU_ALIGNMENT_PADDING.get((page, text), (0, 0))
    return (
        f"{menu_label(icon, text)}"
        f"{HAIR_SPACE * hair_padding}"
        f"{BRAILLE_PATTERN_BLANK * braille_padding}"
    )
