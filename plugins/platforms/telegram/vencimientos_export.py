"""Build the downloadable ARCA due-date artifacts used by Telegram."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, timedelta
from pathlib import Path
import re
import uuid


HEADERS = (
    "Impuesto", "Concepto", "Período", "Anticipo/Cuota",
    "Tipo de operación", "Vencimiento", "Formularios", "Estado",
)
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _safe_excel_text(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _due_date(row: dict) -> date:
    return date.fromisoformat(str(row["fecha"]))


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value)


def write_xlsx(output: Path, rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    sheet.title = "Vencimientos"
    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([
            _safe_excel_text(_text(row, "impuesto")),
            _safe_excel_text(_text(row, "concepto")),
            _safe_excel_text(_text(row, "periodo")),
            _safe_excel_text(_text(row, "anticipo_cuota")),
            _safe_excel_text(_text(row, "tipo")),
            _due_date(row),
            _safe_excel_text(_text(row, "formularios")),
            _safe_excel_text(_text(row, "estado")),
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet["F"][1:]:
        cell.number_format = "dd/mm/yyyy"
    for column in sheet.columns:
        letter = column[0].column_letter
        sheet.column_dimensions[letter].width = min(
            max(len(str(cell.value or "")) for cell in column) + 2, 48
        )
    book.save(output)
    book.close()


def _ics_escape(value: object) -> str:
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n")
            .replace("\r", "\\n").replace("\n", "\\n"))


def _ics_uid(contributor_id: int, row: dict) -> str:
    stable = "\x1f".join(str(row[key]) for key in (
        "id_impuesto", "id_concepto", "periodo", "anticipo_cuota", "tipo",
    ))
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
    return f"{contributor_id}-{digest}@contabot.vectux.com"


def write_ics(output: Path, contributor_id: int, rows: list[dict]) -> None:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//ContaBot//Vencimientos ARCA//ES", "CALSCALE:GREGORIAN",
    ]
    for row in rows:
        due = _due_date(row)
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{_ics_uid(contributor_id, row)}",
            f"DTSTART;VALUE=DATE:{due:%Y%m%d}",
            f"DTEND;VALUE=DATE:{due + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_ics_escape(_text(row, 'impuesto') + ' · ' + _text(row, 'concepto'))}",
            f"DESCRIPTION:{_ics_escape('Período ' + _text(row, 'periodo') + ' · Estado ' + _text(row, 'estado'))}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    output.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))


def generate(kind: str, output: Path, contributor_id: int, slug: str, rows: list[dict]) -> None:
    if kind not in {"xlsx", "ics"} or not SLUG.fullmatch(slug):
        raise ValueError("invalid_export_request")
    if contributor_id <= 0 or not rows or output.exists() or output.is_symlink():
        raise ValueError("invalid_export_target")
    parent = output.parent.resolve(strict=True)
    if not parent.is_dir() or output.parent != parent:
        raise ValueError("invalid_export_directory")
    expected = f"{slug}-vencimientos-arca.{kind}"
    if output.name != expected:
        raise ValueError("invalid_export_name")
    temporary = parent / f".{output.name}.{uuid.uuid4().hex}.tmp"
    try:
        if kind == "xlsx":
            write_xlsx(temporary, rows)
        else:
            write_ics(temporary, contributor_id, rows)
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("xlsx", "ics"))
    parser.add_argument("output", type=Path)
    parser.add_argument("contributor_id", type=int)
    parser.add_argument("slug")
    args = parser.parse_args()
    rows = json.load(__import__("sys").stdin)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("invalid_export_rows")
    generate(args.kind, args.output, args.contributor_id, args.slug, rows)
    print(json.dumps({"ok": True, "rows": len(rows), "kind": args.kind}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
