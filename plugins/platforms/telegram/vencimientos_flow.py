"""Consulta y avisos programados de vencimientos ARCA por Telegram."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import menu_label


@dataclass
class State:
    user_id: str
    nonce: str
    stage: str = "search"
    created_at: float = 0.0


class VencimientosFlow:
    TTL = 600

    def __init__(self) -> None:
        self.states: dict[str, State] = {}

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
          SELECT json_build_object('id',id_contribuyente,'nombre',nombre_legal,'slug',slug)::text
            FROM console.vw_vencimientos_telegram
           WHERE telegram_id={int(telegram_id)}
             AND (lower(nombre_legal) LIKE '%'||lower({literal})||'%'
                  OR slug=lower({literal}))
           GROUP BY id_contribuyente,nombre_legal,slug ORDER BY nombre_legal LIMIT 12;
        """)

    def _calendar(self, telegram_id: int, contributor_id: int) -> list[dict[str, Any]]:
        return self._query(f"""
          SELECT json_build_object('impuesto',impuesto,'concepto',concepto,'periodo',periodo,
                 'tipo',tipo_operacion,'fecha',to_char(fecha_vencimiento,'DD/MM/YYYY'),'estado',estado)::text
            FROM console.vw_vencimientos_telegram
           WHERE telegram_id={int(telegram_id)} AND id_contribuyente={int(contributor_id)}
           ORDER BY fecha_vencimiento,id_vencimiento LIMIT 40;
        """)

    @staticmethod
    def _cancel(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"ve:cancel:{nonce}")]])

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
            await query.edit_message_text("Vencimientos ARCA\nIngresá el nombre o slug del contribuyente.", reply_markup=self._cancel(state.nonce))
            return True
        if not state or len(parts) < 3 or parts[2] != state.nonce:
            await query.answer("Esta consulta venció. Iniciá una nueva.")
            return True
        if parts[1] == "cancel":
            self.states.pop(key, None); await query.answer(); await query.edit_message_text("Consulta cancelada.")
            return True
        if parts[1] == "select" and len(parts) == 4 and parts[3].isdigit():
            await query.answer(); await self._show(adapter, chat_id, thread_id, int(user_id), int(parts[3])); self.states.pop(key, None)
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
            await message.reply_text("No encontré un contribuyente autorizado con ese nombre o slug.", reply_markup=self._cancel(state.nonce)); return True
        if len(rows) == 1:
            self.states.pop(key, None); await self._show(adapter, message.chat_id, getattr(message, "message_thread_id", None), int(state.user_id), int(rows[0]["id"])); return True
        keyboard = [[InlineKeyboardButton(menu_label("👤", row["nombre"]), callback_data=f"ve:select:{state.nonce}:{row['id']}")] for row in rows]
        keyboard += list(self._cancel(state.nonce).inline_keyboard)
        await message.reply_text("Encontré varias coincidencias. Elegí una:", reply_markup=InlineKeyboardMarkup(keyboard))
        return True

    async def _show(self, adapter, chat_id, thread_id, telegram_id: int, contributor_id: int) -> None:
        rows = await asyncio.to_thread(self._calendar, telegram_id, contributor_id)
        if not rows:
            text = "No hay vencimientos ARCA publicados para ese contribuyente."
        else:
            rendered = [f"• {r['fecha']} · {r['impuesto']} · {r['concepto']} · {r['tipo']} · {r['estado']}" for r in rows]
            text = "Vencimientos ARCA\n\n" + "\n".join(rendered)
        kwargs = {"chat_id": chat_id, "text": text}
        if thread_id is not None: kwargs["message_thread_id"] = thread_id
        await adapter._bot.send_message(**kwargs)

    def _claim(self) -> list[dict[str, Any]]:
        return self._query("""
          WITH due AS (
            SELECT a.id_aviso FROM console.tbl_vencimientos_avisos a
             WHERE a.canal='telegram' AND a.estado IN ('pendiente','fallido')
               AND a.disponible_desde<=now() AND a.intentos<5
             ORDER BY a.id_aviso FOR UPDATE SKIP LOCKED LIMIT 20
          ), claimed AS (
            UPDATE console.tbl_vencimientos_avisos a SET estado='enviando',intentos=intentos+1,actualizado_en=now()
             FROM due WHERE a.id_aviso=due.id_aviso RETURNING a.*
          )
          SELECT json_build_object('id',c.id_aviso,'chat_id',v.telegram_id,'impuesto',v.impuesto,
                 'concepto',v.concepto,'fecha',to_char(c.fecha_vencimiento,'DD/MM/YYYY'))::text
            FROM claimed c JOIN console.vw_vencimientos_telegram v
              ON v.id_usuario=c.id_usuario AND v.id_contribuyente=c.id_contribuyente
             AND v.id_impuesto=c.id_impuesto AND v.id_concepto=c.id_concepto
             AND v.periodo=c.periodo AND v.anticipo_cuota=c.anticipo_cuota
             AND v.tipo_operacion=c.tipo_operacion;
        """)

    def _finish_notice(self, notice_id: int, sent: bool) -> None:
        result = "enviado" if sent else "telegram_no_enviado"
        self._query(f"""
          UPDATE console.tbl_vencimientos_avisos SET estado='{'enviado' if sent else 'fallido'}',
                 enviado_en=CASE WHEN {str(sent).lower()} THEN now() ELSE enviado_en END,
                 ultimo_resultado='{result}',
                 disponible_desde=CASE WHEN {str(sent).lower()} THEN disponible_desde ELSE now()+interval '1 day' END,
                 actualizado_en=now() WHERE id_aviso={int(notice_id)} RETURNING '{{}}'::json::text;
        """)

    async def notification_loop(self, adapter) -> None:
        while True:
            try:
                for row in await asyncio.to_thread(self._claim):
                    sent = False
                    try:
                        await adapter._bot.send_message(chat_id=row["chat_id"], text=f"Recordatorio ContaBot\n{row['fecha']} · {row['impuesto']} · {row['concepto']}")
                        sent = True
                    finally:
                        await asyncio.to_thread(self._finish_notice, int(row["id"]), sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(60)
