"""Single authorization boundary for Telegram fiscal operations."""
from __future__ import annotations

import base64
import re
from typing import Any, Callable


ACCOUNT_NOT_LINKED_MESSAGE = (
    "Tu cuenta de Telegram todavía no está vinculada a un usuario de ContaBot. "
    "Pedile al administrador del estudio que complete la vinculación."
)


class AccountNotLinked(RuntimeError):
    """The allowlisted Telegram identity has no unique active console actor."""


class InvalidCuit(ValueError):
    """The supplied CUIT has an invalid checksum."""


def valid_cuit(value: str) -> bool:
    digits = re.sub(r"[^0-9]", "", value or "")
    if len(digits) != 11:
        return False
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(digit) * weight for digit, weight in zip(digits[:10], weights)) % 11
    check = 11 - remainder
    expected = 0 if check == 11 else 9 if check == 10 else check
    return int(digits[-1]) == expected


def validate_search_term(value: str) -> str:
    term = (value or "").strip()
    digits = re.sub(r"[^0-9]", "", term)
    if digits and re.fullmatch(r"[0-9.\-\s]+", term) and len(digits) == 11 and not valid_cuit(digits):
        raise InvalidCuit("CUIT_INVALID_CHECKSUM")
    return term


def _literal(value: str) -> str:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"convert_from(decode('{encoded}','base64'),'UTF8')"


class FiscalScope:
    """Resolve only contributors visible to one Telegram actor."""

    def __init__(self, query: Callable[[str], list[Any]], entity: str) -> None:
        self.query = query
        self.entity = entity

    @staticmethod
    def _telegram_id(value: Any) -> int:
        text = str(value)
        if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
            raise AccountNotLinked("telegram_actor_invalid")
        return int(text)

    def require_actor(self, telegram_id: Any) -> None:
        actor = self._telegram_id(telegram_id)
        rows = self.query(
            "SELECT json_build_object('linked',"
            f"console.fn_actor_telegram_vinculado({actor}))::text;"
        )
        if len(rows) != 1 or rows[0] != {"linked": True}:
            raise AccountNotLinked("telegram_actor_not_linked")

    def search(self, telegram_id: Any, term: str) -> list[dict[str, Any]]:
        actor = self._telegram_id(telegram_id)
        normalized = validate_search_term(term)
        return self._resolve(actor, _literal(normalized), "NULL")

    def by_id(self, telegram_id: Any, contributor_id: int) -> list[dict[str, Any]]:
        actor = self._telegram_id(telegram_id)
        if type(contributor_id) is not int or contributor_id <= 0:
            return []
        return self._resolve(actor, "NULL", str(contributor_id))

    def _resolve(self, actor: int, term_sql: str, contributor_sql: str) -> list[dict[str, Any]]:
        rows = self.query(f"""
          SELECT json_build_object(
            'id',s.id_contribuyente,'nombre',s.nombre_legal,'cuit',s.cuit,'slug',s.slug,
            'study_id',s.id_estudio,'relation_id',s.id_representacion,
            'relation_revision',s.revision_representacion,
            'verified',s.fecha_verificacion IS NOT NULL,
            'representative_id',s.id_representante,'holder_cuit',s.cuit_representante
          )::text
          FROM console.fn_buscar_contribuyente_fiscal(
            {actor},{_literal(self.entity)},{term_sql},{contributor_sql}
          ) s;
        """)
        safe: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not valid_cuit(str(row.get("cuit", ""))) or not valid_cuit(str(row.get("holder_cuit", ""))):
                continue
            safe.append(row)
        return safe


def mark_verified_sql(telegram_id: Any, item: dict[str, Any], entity: str, verified_cuit: str) -> str:
    actor = FiscalScope._telegram_id(telegram_id)
    relation_id = int(item["relation_id"])
    revision = int(item["relation_revision"])
    expected = str(item["cuit"])
    if not valid_cuit(verified_cuit) or verified_cuit != expected:
        raise InvalidCuit("VERIFIED_SUBJECT_MISMATCH")
    return (
        "SELECT json_build_object('verified',console.fn_verificar_representacion_fiscal("
        f"{actor},{relation_id},{revision},{_literal(entity)},{_literal(expected)}))::text;"
    )


def assert_marked(rows: list[Any]) -> None:
    if len(rows) != 1 or rows[0] != {"verified": True}:
        raise RuntimeError("REPRESENTATION_VERIFICATION_FAILED")
