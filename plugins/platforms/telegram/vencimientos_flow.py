"""Consulta manual de vencimientos ARCA por Telegram."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import aligned_menu_label, menu_label
from plugins.platforms.telegram.ccma_artifact import publish_named, validate_identity


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

    def __init__(self, runtime_python: Path | None = None, clients_root: Path | None = None) -> None:
        self.states: dict[str, State] = {}
        self.runtime_python = Path(runtime_python) if runtime_python else None
        self.clients_root = Path(clients_root) if clients_root else Path(
            os.environ.get("CONTABOT_CLIENTES_ROOT", Path.home() / "clientes")
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
          SELECT json_build_object('id',id_contribuyente,'nombre',nombre_legal,'slug',slug,
                 'cuit',regexp_replace(cuit,'[^0-9]','','g'))::text
            FROM console.vw_vencimientos_telegram
           WHERE telegram_id={int(telegram_id)}
             AND (lower(nombre_legal) LIKE '%'||lower({literal})||'%'
                  OR slug=lower({literal})
                  OR regexp_replace(cuit,'[^0-9]','','g')=
                     regexp_replace({literal},'[^0-9]','','g'))
           GROUP BY id_contribuyente,nombre_legal,slug ORDER BY nombre_legal LIMIT 12;
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
            raise LookupError("vencimientos_empty")
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
            await query.edit_message_text("Vencimientos ARCA\nIngresá el nombre, CUIT o alias del contribuyente.", reply_markup=self._cancel(state.nonce))
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
        if not rows:
            await message.reply_text("No encontré un contribuyente autorizado con ese nombre, CUIT o alias.", reply_markup=self._cancel(state.nonce)); return True
        if len(rows) == 1:
            self._select_subject(state, rows[0])
            await self._ask_format(message, state, edit=False)
            return True
        state.candidates = {int(row["id"]): row for row in rows}
        keyboard = [[InlineKeyboardButton(menu_label("👤", row["nombre"]), callback_data=f"ve:select:{state.nonce}:{row['id']}")] for row in rows]
        keyboard += list(self._cancel(state.nonce).inline_keyboard)
        await message.reply_text("Encontré varias coincidencias. Elegí una:", reply_markup=InlineKeyboardMarkup(keyboard))
        return True
