"""Shared contributor chooser for Telegram workflows.

The data source remains owned by each workflow. This module owns the common
zero/one/many decision, the offered-candidate snapshot and its inline picker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import menu_label


CONTRIBUTOR_PROMPT = "Ingresá nombre, CUIT o alias del contribuyente."
MULTIPLE_CONTRIBUTORS_TEXT = "Encontré varias coincidencias. Elegí un contribuyente:"


def visible_cuit(cuit: str) -> str:
    return f"{cuit[:2]}-******-{cuit[-1:]}" if re.fullmatch(r"\d{11}", cuit or "") else "CUIT oculto"


@dataclass(frozen=True)
class ContributorOffer:
    """Immutable snapshot of the candidates actually offered to the user."""

    candidates: dict[int, dict[str, Any]]

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]]) -> "ContributorOffer":
        candidates: dict[int, dict[str, Any]] = {}
        for source in rows:
            if not isinstance(source, dict):
                raise ValueError("contributor_candidate_invalid")
            try:
                item_id = int(source["id"])
                name = str(source["nombre"]).strip()
                cuit = re.sub(r"[^0-9]", "", str(source["cuit"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("contributor_candidate_invalid") from exc
            if item_id <= 0 or not name or not re.fullmatch(r"\d{11}", cuit):
                raise ValueError("contributor_candidate_invalid")
            if item_id in candidates:
                raise ValueError("contributor_candidate_duplicate")
            candidates[item_id] = {**source, "cuit": cuit}
        return cls(candidates)

    @property
    def status(self) -> str:
        if not self.candidates:
            return "empty"
        return "single" if len(self.candidates) == 1 else "multiple"

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return tuple(self.candidates)

    @property
    def single(self) -> dict[str, Any] | None:
        return next(iter(self.candidates.values())) if len(self.candidates) == 1 else None

    def pick(self, item_id: int) -> dict[str, Any] | None:
        return self.candidates.get(item_id)

    def keyboard(
        self,
        *,
        callback_prefix: str,
        nonce: str,
        cancel_text: str,
        button_factory=None,
        markup_factory=None,
    ) -> InlineKeyboardMarkup:
        if not re.fullmatch(r"[a-z]{2}", callback_prefix) or not nonce:
            raise ValueError("contributor_callback_invalid")
        button_factory = button_factory or InlineKeyboardButton
        markup_factory = markup_factory or InlineKeyboardMarkup
        rows = [
            [button_factory(
                menu_label("👤", f"{row['nombre']} — {visible_cuit(str(row['cuit']))}"),
                callback_data=f"{callback_prefix}:select:{nonce}:{item_id}",
            )]
            for item_id, row in self.candidates.items()
        ]
        rows.append([button_factory(cancel_text, callback_data=f"{callback_prefix}:cancel:{nonce}")])
        return markup_factory(rows)
