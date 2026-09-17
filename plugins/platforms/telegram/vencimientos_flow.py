"""Consulta manual de vencimientos ARCA por Telegram."""
from __future__ import annotations

import asyncio
import base64
import csv
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import aligned_menu_label, menu_label
from plugins.platforms.telegram.ccma_artifact import publish_named, validate_identity
from plugins.platforms.telegram.contributor_selector import (
    CONTRIBUTOR_PROMPT,
    MULTIPLE_CONTRIBUTORS_TEXT,
    ContributorOffer,
)


@dataclass
class State:
    user_id: str
    nonce: str
    stage: str = "search"
    created_at: float = 0.0
    contributor_id: int | None = None
    contributor_name: str = ""
    contributor_slug: str = ""
    contributor_cuit: str = ""
    candidates: dict[int, dict[str, Any]] | None = None


class VencimientosFlow:
    TTL = 600
    SOURCE_HEADERS = [
        "ID Impuesto", "Impuesto", "ID Concepto", "Concepto", "Período",
        "Anticipo/Cuota", "Tipo Operación", "Vencimiento", "Formularios",
    ]

    def __init__(self, runtime_python: Path | None = None, clients_root: Path | None = None,
                 source_script: Path | None = None, source_map: Path | None = None) -> None:
        self.states: dict[str, State] = {}
        self.runtime_python = Path(runtime_python) if runtime_python else None
        self.clients_root = Path(clients_root) if clients_root else Path(
            os.environ.get("CONTABOT_CLIENTES_ROOT", Path.home() / "clientes")
        )
        self.source_script = Path(source_script) if source_script else Path(
            os.environ.get(
                "CONTABOT_VENCIMIENTOS_SCRIPT",
                "/srv/contabot-console/integrations/regenerar_vencimientos_arca.py",
            )
        )
        self.source_map = Path(source_map) if source_map else Path(
            os.environ.get(
                "CONTABOT_VENCIMIENTOS_MAP",
                "/home/pancho/hermes-workspace/vectux.com/root/mapa_impuestos.json",
            )
        )

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _connection_args() -> tuple[list[str], dict[str, str]]:
        values = {
            "host": os.getenv("CONTABOT_ROUTER_CATALOG_HOST", ""),
            "port": os.getenv("CONTABOT_ROUTER_CATALOG_PORT", ""),
            "database": os.getenv("CONTABOT_ROUTER_CATALOG_DATABASE", ""),
            "user": os.getenv("CONTABOT_ROUTER_CATALOG_USER", ""),
            "pgpass": os.getenv("CONTABOT_ROUTER_CATALOG_PGPASSFILE", ""),
        }
        if not all(values.values()) or not values["port"].isdigit() or not os.path.isabs(values["pgpass"]):
            raise RuntimeError("vencimientos_database_unavailable")
        args = ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", values["host"],
                "-p", values["port"], "-U", values["user"], "-d", values["database"]]
        env = os.environ.copy(); env["PGPASSFILE"] = values["pgpass"]
        return args, env

    @classmethod
    def _query(cls, sql: str) -> list[dict[str, Any]]:
        args, env = cls._connection_args()
        run = subprocess.run(args + ["-c", sql], env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=15, check=False)
        if run.returncode:
            raise RuntimeError("vencimientos_database_query_failed")
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    @staticmethod
    def _literal(value: str) -> str:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"convert_from(decode('{encoded}','base64'),'UTF8')"

    def _search(self, telegram_id: int, term: str) -> list[dict[str, Any]]:
        literal = self._literal(term.strip())
        return self._query(f"""
          SELECT json_build_object('id',id_contribuyente,'nombre',nombre_legal,
                 'slug',slug,'cuit',cuit)::text
            FROM console.fn_buscar_contribuyente_telegram(
                 {int(telegram_id)},{literal});
        """)

    def _calendar(self, telegram_id: int, contributor_id: int) -> list[dict[str, Any]]:
        return self._query(f"""
          SELECT json_build_object('id_impuesto',id_impuesto,'impuesto',impuesto,
                 'id_concepto',id_concepto,'concepto',concepto,'periodo',periodo,
                 'anticipo_cuota',anticipo_cuota,'tipo',tipo_operacion,
                 'fecha',to_char(fecha_vencimiento,'YYYY-MM-DD'),
                 'formularios',formularios,'estado',estado)::text
            FROM console.vw_vencimientos_telegram
           WHERE telegram_id={int(telegram_id)} AND id_contribuyente={int(contributor_id)}
           ORDER BY fecha_vencimiento,id_vencimiento;
        """)

    @staticmethod
    def _safe_source_file(path: Path) -> None:
        if not path.is_absolute():
            raise RuntimeError("vencimientos_source_path_invalid")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("vencimientos_source_file_invalid")
        if info.st_mode & 0o022:
            raise RuntimeError("vencimientos_source_permissions_invalid")

    def _source_calendar(self, cuit: str) -> list[dict[str, Any]]:
        python = self.runtime_python
        if (python is None or not python.is_absolute() or not python.is_file()
                or not os.access(python, os.X_OK)):
            raise RuntimeError("vencimientos_source_runtime_unavailable")
        self._safe_source_file(self.source_script)
        self._safe_source_file(self.source_map)
        with tempfile.TemporaryDirectory(prefix="contabot-vencimientos-source-") as directory:
            root = Path(directory)
            requested = root / "vencimientos.csv"
            process = subprocess.Popen(
                [str(python), "-I", "-B", str(self.source_script), cuit,
                 str(self.source_map), str(requested)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                start_new_session=True,
            )
            try:
                stdout, _ = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                raise RuntimeError("vencimientos_source_timeout") from None
            if process.returncode:
                raise RuntimeError("vencimientos_source_failed")
            try:
                result = json.loads(stdout)
                output = Path(result["output"])
            except (json.JSONDecodeError, KeyError, TypeError):
                raise RuntimeError("vencimientos_source_response_invalid") from None
            if output.parent != root:
                raise RuntimeError("vencimientos_source_output_outside_staging")
            self._safe_source_file(output)
            with output.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter=";")
                if reader.fieldnames != self.SOURCE_HEADERS:
                    raise RuntimeError("vencimientos_source_columns_invalid")
                rows: list[dict[str, Any]] = []
                keys: set[tuple[str, ...]] = set()
                for raw in reader:
                    try:
                        due = datetime.strptime(raw["Vencimiento"].strip(), "%d/%m/%Y").date()
                    except (ValueError, AttributeError):
                        raise RuntimeError("vencimientos_source_date_invalid") from None
                    row = {
                        "id_impuesto": raw["ID Impuesto"].strip(),
                        "impuesto": raw["Impuesto"].strip(),
                        "id_concepto": raw["ID Concepto"].strip(),
                        "concepto": raw["Concepto"].strip(),
                        "periodo": raw["Período"].strip(),
                        "anticipo_cuota": raw["Anticipo/Cuota"].strip(),
                        "tipo": raw["Tipo Operación"].strip(),
                        "fecha": due.isoformat(),
                        "formularios": raw["Formularios"].strip(),
                        "estado": "pendiente",
                    }
                    key = tuple(row[name] for name in (
                        "id_impuesto", "id_concepto", "periodo", "anticipo_cuota", "tipo",
                    ))
                    if not row["id_impuesto"] or not row["id_concepto"] or key in keys:
                        raise RuntimeError("vencimientos_source_identity_invalid")
                    keys.add(key)
                    rows.append(row)
            if not rows or result.get("rows") != len(rows):
                raise RuntimeError("vencimientos_source_rows_invalid")
            return rows

    @staticmethod
    def _cancel(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"ve:cancel:{nonce}")]])

    @staticmethod
    def _formats(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(aligned_menu_label("vencimientos_formato", "📊", "Excel"), callback_data=f"ve:format:{nonce}:xlsx")],
            [InlineKeyboardButton(aligned_menu_label("vencimientos_formato", "📅", "ICS para Google Calendar"), callback_data=f"ve:format:{nonce}:ics")],
            [InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"ve:cancel:{nonce}")],
        ])

    @staticmethod
    def _candidate_keyboard(candidates: list[dict[str, Any]], nonce: str) -> InlineKeyboardMarkup:
        return ContributorOffer.from_rows(candidates).keyboard(
            callback_prefix="ve",
            nonce=nonce,
            cancel_text=menu_label("❌", "Cancelar"),
            button_factory=InlineKeyboardButton,
            markup_factory=InlineKeyboardMarkup,
        )

    @staticmethod
    def _select_subject(state: State, row: dict[str, Any]) -> None:
        state.contributor_id = int(row["id"])
        state.contributor_name = str(row["nombre"])
        state.contributor_slug = str(row["slug"])
        state.contributor_cuit = str(row["cuit"])
        state.stage = "format"

    def _destination(self, state: State, rows: list[dict[str, Any]]) -> Path:
        validate_identity(state.contributor_slug, state.contributor_cuit)
        years: set[str] = set()
        for row in rows:
            value = str(row.get("fecha", ""))
            parsed = date.fromisoformat(value)
            years.add(str(parsed.year))
        if not years:
            raise ValueError("vencimientos_dates_missing")
        base = self.clients_root / state.contributor_slug / state.contributor_cuit / "arca"
        return base / next(iter(years)) / "anual" / "consultas" if len(years) == 1 else base / "consultas"

    async def _ask_format(self, target, state: State, *, edit: bool) -> None:
        text = f"Vencimientos ARCA · {state.contributor_name}\nElegí el archivo que querés recibir."
        method = target.edit_message_text if edit else target.reply_text
        await method(text, reply_markup=self._formats(state.nonce))

    def _generate(self, kind: str, output: Path, contributor_id: int, slug: str,
                  rows: list[dict[str, Any]]) -> None:
        python = self.runtime_python
        script = Path(__file__).with_name("vencimientos_export.py")
        if (python is None or not python.is_absolute() or not python.is_file()
                or not os.access(python, os.X_OK) or not script.is_file()):
            raise RuntimeError("vencimientos_export_runtime_unavailable")
        run = subprocess.run(
            [str(python), "-I", "-B", str(script), kind, str(output),
             str(contributor_id), slug],
            input=json.dumps(rows, ensure_ascii=False), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        )
        if run.returncode or not output.is_file() or output.is_symlink() or output.stat().st_size == 0:
            raise RuntimeError("vencimientos_export_failed")

    async def _deliver(self, adapter, chat_id, thread_id, telegram_id: int,
                       state: State, kind: str) -> None:
        if state.contributor_id is None:
            raise RuntimeError("vencimientos_subject_missing")
        rows = await asyncio.to_thread(self._calendar, telegram_id, state.contributor_id)
        if not rows:
            rows = await asyncio.to_thread(self._source_calendar, state.contributor_cuit)
        with tempfile.TemporaryDirectory(prefix="contabot-vencimientos-") as directory:
            output = Path(directory) / f"{state.contributor_slug}-vencimientos-arca.{kind}"
            await asyncio.to_thread(
                self._generate, kind, output, state.contributor_id,
                state.contributor_slug, rows,
            )
            destination = self._destination(state, rows)
            output = await asyncio.to_thread(publish_named, output, destination, output.name)
            metadata = {"thread_id": thread_id} if thread_id is not None else None
            result = await adapter.send_document(
                chat_id=str(chat_id), file_path=str(output), file_name=output.name,
                caption=f"Vencimientos ARCA · {state.contributor_name} · {len(rows)} registro(s).",
                metadata=metadata,
            )
            if not getattr(result, "success", False):
                raise RuntimeError("vencimientos_delivery_failed")

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id: str) -> bool:
        if not data.startswith("ve:"):
            return False
        key = self._key(chat_id, thread_id, user_id)
        parts = data.split(":")
        state = self.states.get(key)
        if len(parts) >= 2 and parts[1] == "start":
            if state and time.monotonic() - state.created_at < self.TTL:
                await query.answer("Completá o cancelá la consulta en curso.")
                return True
            state = State(user_id=user_id, nonce=uuid.uuid4().hex[:10], created_at=time.monotonic())
            self.states[key] = state
            await query.answer()
            await query.edit_message_text(
                f"Vencimientos ARCA\n{CONTRIBUTOR_PROMPT}",
                reply_markup=self._cancel(state.nonce),
            )
            return True
        if not state or len(parts) < 3 or parts[2] != state.nonce:
            await query.answer("Esta consulta venció. Iniciá una nueva.")
            return True
        if parts[1] == "cancel":
            self.states.pop(key, None); await query.answer(); await query.edit_message_text("Consulta cancelada.")
            return True
        if parts[1] == "select" and len(parts) == 4 and parts[3].isdigit():
            candidate = (state.candidates or {}).get(int(parts[3]))
            if not candidate or state.stage != "search":
                await query.answer("Esta opción ya no está disponible.")
                return True
            self._select_subject(state, candidate)
            await query.answer(); await self._ask_format(query, state, edit=True)
            return True
        if parts[1] == "format" and len(parts) == 4 and parts[3] in {"xlsx", "ics"}:
            if state.stage != "format":
                await query.answer("Esta opción ya no está disponible.")
                return True
            kind = parts[3]
            await query.answer()
            await query.edit_message_text(f"Preparando {'Excel' if kind == 'xlsx' else 'ICS'}…")
            try:
                await self._deliver(adapter, chat_id, thread_id, int(user_id), state, kind)
            except LookupError:
                self.states.pop(key, None)
                await query.edit_message_text("No hay vencimientos ARCA publicados para ese contribuyente.")
                return True
            except Exception:
                await query.edit_message_text(
                    "No pude generar el archivo. Podés reintentar o cancelar.",
                    reply_markup=self._formats(state.nonce),
                )
                return True
            self.states.pop(key, None)
            await query.edit_message_text(f"{'Excel' if kind == 'xlsx' else 'ICS'} enviado.")
            return True
        await query.answer("Esta opción ya no está disponible.")
        return True

    async def text(self, adapter, message) -> bool:
        user = getattr(message, "from_user", None)
        key = self._key(message.chat_id, getattr(message, "message_thread_id", None), getattr(user, "id", ""))
        state = self.states.get(key)
        if not state:
            return False
        if time.monotonic() - state.created_at > self.TTL:
            self.states.pop(key, None); await message.reply_text("La consulta venció. Iniciá una nueva."); return True
        try:
            rows = await asyncio.to_thread(self._search, int(state.user_id), message.text or "")
        except Exception:
            self.states.pop(key, None); await message.reply_text("No pude consultar los vencimientos. Probá nuevamente más tarde."); return True
        offer = ContributorOffer.from_rows(rows)
        state.candidates = offer.candidates
        if offer.status == "empty":
            await message.reply_text("No encontré un contribuyente autorizado con ese nombre, CUIT o alias.", reply_markup=self._cancel(state.nonce)); return True
        if offer.status == "single":
            self._select_subject(state, offer.single)
            await self._ask_format(message, state, edit=False)
            return True
        await message.reply_text(
            MULTIPLE_CONTRIBUTORS_TEXT,
            reply_markup=self._candidate_keyboard(rows, state.nonce),
        )
        return True
