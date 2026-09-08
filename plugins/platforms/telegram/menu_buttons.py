"""Shared visible-label contract for ContaBot Telegram buttons."""

MENU_SEPARATOR = "  "


def menu_label(icon: str, text: str) -> str:
    """Return an icon label separated by exactly two ASCII spaces."""
    if not icon or not text:
        raise ValueError("menu button icon and text are required")
    if icon != icon.strip() or text != text.strip():
        raise ValueError("menu button icon and text must not have edge whitespace")
    return f"{icon}{MENU_SEPARATOR}{text}"
