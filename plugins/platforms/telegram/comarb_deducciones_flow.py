"""Deducciones de IIBB desde SIFERE Web (Ágora #133): pedido por Telegram, cola COMARB.

El bot sólo encola en la base (console.fn_comarb_deducciones_*_telegram); la lectura la
hace el worker fiscal de la #134, el mismo del panel. El CAPTCHA llega por el puente
existente. La entrega revalida en la base que el usuario siga teniendo acceso al
contribuyente antes de mandar el Excel. Sólo lectura ante COMARB.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import ContributorOffer
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_credentials import (
    lookup_connection,
    sanitized_psql_failure,
    verification_connection,
)
from plugins.platforms.telegram.fiscal_scope import (
    ACCOUNT_NOT_LINKED_MESSAGE,
    AccountNotLinked,
    FiscalScope,
    InvalidCuit,
)
from plugins.platforms.telegram.menu_buttons import menu_label

logger = logging.getLogger(__name__)
_CLIENTS_ROOT = Path(os.environ.get("CONTABOT_CLIENTES_ROOT", Path.home() / "clientes"))
_STATE_TTL_SECONDS = 600
TITLE = "📑 Deducciones de IIBB"
REGIMENES = ("SIRCREB", "SIRCUPA", "SIRTAC", "SIRPEI", "SIRCAR · Retenciones", "SIRCAR · Percepciones")
ETIQUETA = {"SIRCAR · Retenciones": "SIRCAR, retenciones", "SIRCAR · Percepciones": "SIRCAR, percepciones"}
PROGRESO = {"captcha_telegram": "ARCA pidió un CAPTCHA: respondé a la imagen que te mandé.",
            "consulta_en_curso": "CAPTCHA respondido; ingresando a ARCA…"}
RECHAZOS = {
    "sin_permiso": "Tu usuario no tiene permiso para ejecutar consultas. No se consultó COMARB.",
    "sin_acceso": "No se puede consultar ese contribuyente con tu acceso actual. No se consultó COMARB.",
    "clave_sin_verificar": ("La clave ARCA de ese contribuyente, propia o del representante, no está verificada. "
                            "Verificala en Accesos del panel. No se consultó COMARB."),
    "ocupado": "Ya hay una consulta de COMARB en curso en tu estudio. Esperá que termine y volvé a pedirla.",
    "periodo_invalido": "Ingresá un mes ya cerrado como MM/AAAA. Ejemplo: 09/2026.",
    "sin_estudio": "Tu usuario no pertenece a un estudio: pedí la cartera desde el panel.",
    "demasiados": "La cartera supera los 1000 contribuyentes: pedila desde el panel por partes.",
}


def importe(text: Any) -> str:
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return "—"
    sign = "-" if value < 0 else ""
    entero, _, dec = f"{abs(value):.2f}".partition(".")
    return f"{sign}$ {int(entero):,}".replace(",", ".") + "," + dec


def resumen(row: dict[str, Any]) -> str:
    """Texto de cierre para un contribuyente: un renglón por régimen, nunca cero sin aclarar."""
    res = row.get("resultado") or {}
    head = f"{TITLE} · {row['nombre_legal']} · {row['periodo']}"
    if res.get("estado") == "no_cm":
        organismos = res.get("organismos") or []
        detail = "; ".join(f"{o['jurisdiccion']}: {o['organismo']}" for o in organismos)
        return (f"{head}\n\nNo es de Convenio Multilateral en {row['periodo']}: SIFERE no es su fuente y no se consultó COMARB."
                + (f"\nSus retenciones y percepciones salen de {detail}." if detail else ""))
    if row["estado"] != "terminada":
        return f"{head}\n\n{'Cancelada' if row['estado'] == 'cancelada' else 'No se pudo consultar'}. {row.get('motivo') or ''}".rstrip()
    estados = res.get("estados") or {}
    faltan = any(v.get("estado") == "no_verificado" for v in estados.values())
    lines = [head, "", "Terminada con faltantes." if faltan else "Terminada."]
    avisos = res.get("advertencias") or []
    for regimen in REGIMENES:
        e = estados.get(regimen) or {}
        name = ETIQUETA.get(regimen, regimen)
        if e.get("estado") == "con_datos":
            line = f"• {name}: {e.get('filas', 0)} filas · {importe(e.get('importe'))}"
        elif e.get("estado") == "sin_datos_verificado":
            line = f"• {name}: SIFERE no registra deducciones de {regimen} para {row['periodo']}."
        else:
            line = f"• {name}: no se pudo verificar. {e.get('motivo') or ''}".rstrip()
        aviso = next((a.split(": ", 1)[1] for a in avisos if a.startswith(regimen + ":")), None)
        if aviso and e.get("estado") != "con_datos":
            line += f" {aviso}"
        lines.append(line)
    if res.get("posibles_duplicados"):
        lines.append(f"{res['posibles_duplicados']} filas marcadas como posible duplicado; no se borró ninguna.")
    return "\n".join(lines)


@dataclass
class State:
    user_id: str
    nonce: str
    stage: str  # buscar | periodo
    modo: str  # uno | cartera
    contributor: dict[str, Any] | None = None
    offer: ContributorOffer | None = None
    created_at: float = field(default_factory=time.monotonic)


class ComarbDeduccionesFlow:
    def __init__(self, *, query_connection=None, write_connection=None, clients_root: Path | None = None) -> None:
        self.query_connection = query_connection or lookup_connection
        self.write_connection = write_connection or verification_connection
        self.clients_root = Path(clients_root or _CLIENTS_ROOT)
        self.states: dict[str, State] = {}
        self.watchers: dict[int, asyncio.Task] = {}

    # --- base -------------------------------------------------------------
    @staticmethod
    def _run(sql: str, connection) -> list[Any]:
        command, environment = connection()
        run = subprocess.run([*command, "-At", "-v", "VERBOSITY=sqlstate", "-c", sql], env=environment,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        if run.returncode:
            sqlstate, _ = sanitized_psql_failure(run.stderr)
            logger.error("[COMARB-DEDUCCIONES] database query failed sqlstate=%s", sqlstate)
            raise RuntimeError("COMARB_DEDUCCIONES_DATABASE")
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def _query(self, sql: str) -> list[Any]:
        return self._run(sql, self.query_connection)

    def _write(self, sql: str) -> list[Any]:
        return self._run(sql, self.write_connection)

    def _scope(self) -> FiscalScope:
        return FiscalScope(self._query, "ARCA")

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _period(text: str) -> str | None:
        value = (text or "").strip()
        m = re.fullmatch(r"(0?[1-9]|1[0-2])/(\d{4})", value)
        return f"{int(m[1]):02d}/{m[2]}" if m else None

    @staticmethod
    def _back() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("‹", "Deducciones de IIBB"), callback_data="om:comarb_deducciones")],
                                     [InlineKeyboardButton(menu_label("✕", "Cerrar"), callback_data="om:close")]])

    def _pedir_sql(self, actor: int, state: State, period: str) -> str:
        if state.modo == "cartera":
            return f"SELECT console.fn_comarb_deducciones_cartera_telegram({actor},'{period}')::text;"
        return (f"SELECT console.fn_comarb_deducciones_pedir_telegram({actor},"
                f"{int(state.contributor['id'])},'{period}')::text;")

    # --- callbacks ----------------------------------------------------------
    async def callback(self, adapter, query, data: str, chat_id: Any, thread_id: Any, user_id: str) -> bool:
        if not data.startswith("cd:"):
            return False
        parts = data.split(":")
        key = self._key(chat_id, thread_id, user_id)
        if parts[1] in ("uno", "cartera"):
            try:
                await asyncio.to_thread(self._scope().require_actor, user_id)
                await asyncio.to_thread(self._scope().require_permission, user_id, "operaciones.ejecutar")
            except AccountNotLinked:
                await query.answer("Cuenta no vinculada")
                await adapter._bot.send_message(chat_id=chat_id, text=ACCOUNT_NOT_LINKED_MESSAGE)
                return True
            except PermissionError:
                await query.answer("Sin permiso")
                await adapter._bot.send_message(chat_id=chat_id, text=RECHAZOS["sin_permiso"])
                return True
            except Exception:
                await query.answer("No disponible")
                await adapter._bot.send_message(chat_id=chat_id, text="No pude verificar tu acceso en la base. No se consultó COMARB.")
                return True
            nonce = secrets.token_hex(4)
            if parts[1] == "uno":
                self.states[key] = State(user_id, nonce, "buscar", "uno")
                text = "Escribí nombre, CUIT o slug del contribuyente."
            else:
                self.states[key] = State(user_id, nonce, "periodo", "cartera")
                text = ("Cartera de Convenio: se consultan los contribuyentes que ves, de Convenio Multilateral en ese mes "
                        "y con clave ARCA verificada. Recibís un Excel por contribuyente.\n\n"
                        "Ingresá el período como MM/AAAA. Ejemplo: 09/2026.")
            await query.answer()
            await adapter._bot.send_message(chat_id=chat_id, text=text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"cd:cancel:{nonce}")]]))
            return True
        state = self.states.get(key)
        if parts[1] == "cancel":
            if state and len(parts) == 3 and parts[2] == state.nonce:
                self.states.pop(key, None)
            await query.answer("Cancelado")
            await adapter._bot.send_message(chat_id=chat_id, text="Cancelado. No se consultó COMARB.", reply_markup=self._back())
            return True
        if parts[1] == "select" and len(parts) == 4:
            if not state or state.nonce != parts[2] or state.offer is None or not parts[3].isdigit():
                await query.answer("Esa selección ya no está vigente.")
                return True
            picked = state.offer.pick(int(parts[3]))
            fresh = await asyncio.to_thread(self._scope().by_id, user_id, int(parts[3])) if picked else []
            if len(fresh) != 1:
                await query.answer()
                await adapter._bot.send_message(chat_id=chat_id, text=RECHAZOS["sin_acceso"], reply_markup=self._back())
                self.states.pop(key, None)
                return True
            await query.answer()
            await self._ask_period(adapter, chat_id, state, fresh[0])
            return True
        if parts[1] == "stop" and len(parts) == 3 and parts[2].isdigit():
            actor = FiscalScope._telegram_id(user_id)
            try:
                ok = await asyncio.to_thread(self._write, f"SELECT console.fn_comarb_deducciones_cancelar_telegram({actor},{int(parts[2])})::text;")
            except Exception:
                ok = [False]
            await query.answer("Cancelación pedida" if ok == [True] else "La consulta ya terminó o no es tuya.")
            return True
        await query.answer()
        return True

    async def _ask_period(self, adapter, chat_id: Any, state: State, item: dict[str, Any]) -> None:
        state.contributor = item
        state.stage = "periodo"
        state.offer = None
        await adapter._bot.send_message(chat_id=chat_id,
            text=f"{item['nombre']}.\nIngresá el período como MM/AAAA. Ejemplo: 09/2026.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"cd:cancel:{state.nonce}")]]))

    # --- texto -------------------------------------------------------------
    async def text(self, adapter, msg) -> bool:
        user_id = str(getattr(msg.from_user, "id", ""))
        key = self._key(msg.chat_id, getattr(msg, "message_thread_id", None), user_id)
        state = self.states.get(key)
        if state is None:
            return False
        if time.monotonic() - state.created_at > _STATE_TTL_SECONDS:
            self.states.pop(key, None)
            await msg.reply_text("Pasaron más de 10 minutos: volvé a empezar desde el menú. No se consultó COMARB.")
            return True
        if state.stage == "buscar":
            try:
                rows = await asyncio.to_thread(self._scope().search, user_id, msg.text)
            except InvalidCuit:
                await msg.reply_text("El CUIT no es válido. Revisalo y escribilo de nuevo.")
                return True
            except Exception:
                await msg.reply_text("No pude buscar en la base. No se consultó COMARB.")
                return True
            offer = ContributorOffer.from_rows(rows)
            if offer.status == "empty":
                self.states.pop(key, None)
                await msg.reply_text(RECHAZOS["sin_acceso"], reply_markup=self._back())
            elif offer.status == "single":
                await self._ask_period(adapter, msg.chat_id, state, offer.single)
            else:
                state.offer = offer
                await msg.reply_text("Hay más de un contribuyente con ese dato. Elegí uno:",
                    reply_markup=offer.keyboard(callback_prefix="cd", nonce=state.nonce,
                                                cancel_text=menu_label("❌", "Cancelar")))
            return True
        period = self._period(msg.text)
        if period is None:
            await msg.reply_text(RECHAZOS["periodo_invalido"])
            return True
        self.states.pop(key, None)
        actor = FiscalScope._telegram_id(user_id)
        try:
            rows = await asyncio.to_thread(self._write, self._pedir_sql(actor, state, period))
            answer = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
        except Exception:
            answer = None
        if answer is None:
            await msg.reply_text("No pude registrar el pedido en la base. No se consultó COMARB.", reply_markup=self._back())
            return True
        estado = answer.get("estado")
        if estado == "no_cm":
            organismos = answer.get("organismos") or []
            detail = "; ".join(f"{o['jurisdiccion']}: {o['organismo']} ({o['servicio']})" for o in organismos)
            await msg.reply_text(f"{state.contributor['nombre']} no es de Convenio Multilateral en {period}: SIFERE no es su fuente y no se consultó COMARB."
                                 + (f"\nSus retenciones y percepciones salen de {detail}." if detail else "\nLa ficha no indica su jurisdicción de Ingresos Brutos."),
                                 reply_markup=self._back())
            return True
        if estado == "sin_candidatos":
            extra = f" {answer.get('sin_clave')} de Convenio no tienen clave ARCA verificada." if answer.get("sin_clave") else ""
            await msg.reply_text(f"No hay contribuyentes de Convenio Multilateral con clave ARCA verificada para {period}.{extra} No se consultó COMARB.",
                                 reply_markup=self._back())
            return True
        if estado != "encolada":
            await msg.reply_text(RECHAZOS.get(estado, "No se pudo iniciar la consulta. No se consultó COMARB."), reply_markup=self._back())
            return True
        jid = int(answer["id_lectura"])
        who = state.contributor["nombre"] if state.modo == "uno" else f"Cartera de Convenio ({answer.get('solicitados')})"
        extra = (f"\n{answer['sin_clave']} de Convenio quedan afuera por no tener clave ARCA verificada."
                 if state.modo == "cartera" and answer.get("sin_clave") else "")
        status = await msg.reply_text(f"{TITLE} · {who} · {period}\nEn espera de la consulta de SIFERE.{extra}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"cd:stop:{jid}")]]))
        self.watchers[jid] = asyncio.create_task(self._watch(status, actor, jid, f"{TITLE} · {who} · {period}", extra))
        return True

    async def _watch(self, message, actor: int, jid: int, head: str, extra: str) -> None:
        """Avance en el mismo mensaje. El resultado lo manda la entrega, no este seguimiento."""
        last = None
        try:
            for _ in range(1200):
                await asyncio.sleep(3)
                try:
                    rows = await asyncio.to_thread(self._query, f"SELECT console.fn_comarb_deducciones_estado_telegram({actor},{jid})::text;")
                except Exception:
                    continue
                if len(rows) != 1 or not isinstance(rows[0], dict):
                    return
                st = rows[0]
                actual = st.get("actual") or {}
                if st["estado"] in ("terminada", "error", "cancelada"):
                    text = f"{head}\nConsulta terminada: {st['terminados']} de {st['solicitados']}. Te mando el resultado."
                    markup = None
                else:
                    now = PROGRESO.get(actual.get("progreso"), actual.get("progreso") or "En espera.")
                    text = (f"{head}\n{st['terminados']} de {st['solicitados']} listos." if st["solicitados"] > 1 else head) \
                        + (f"\n{actual['contribuyente']} · " if actual and st["solicitados"] > 1 else "\n") + now + extra
                    markup = message.reply_markup
                if text != last:
                    last = text
                    try:
                        await message.edit_text(text, reply_markup=markup)
                    except Exception:
                        pass
                if markup is None:
                    return
        finally:
            self.watchers.pop(jid, None)


class ComarbDeduccionesEntregas:
    """Entrega lo terminado: resumen y Excel, sólo si el acceso sigue vigente al entregar."""

    def __init__(self, flow: ComarbDeduccionesFlow) -> None:
        self.flow = flow

    async def poll(self, bot) -> None:
        rows = await asyncio.to_thread(self.flow._write,
            "SELECT row_to_json(e)::text FROM console.fn_comarb_deducciones_entregas_telegram() e;")
        for row in rows:
            if not isinstance(row, dict):
                continue
            chat = int(row["telegram_id"])
            # Reclamar antes de enviar: una reconexión nunca repite la entrega.
            claimed = await asyncio.to_thread(self.flow._write,
                f"SELECT console.fn_comarb_deducciones_entregada_telegram({int(row['id_lectura'])},{int(row['id_contribuyente'])})::text;")
            if claimed != [True]:
                continue
            if not row.get("autorizado"):
                await bot.send_message(chat_id=chat, text=(f"{TITLE} · {row['periodo']}: la consulta terminó, pero tu usuario ya no "
                                                          "tiene acceso a ese contribuyente. No se entregó el resultado."))
            else:
                await bot.send_message(chat_id=chat, text=resumen(row))
                if row.get("ruta"):
                    try:
                        path = archivo_guardado(None, self.flow.clients_root, row["ruta"], row.get("sha256"))
                    except ValueError:
                        path = None
                    if path is None or path.suffix.lower() != ".xlsx":
                        await bot.send_message(chat_id=chat, text="El Excel quedó en Documentos, pero no se pudo verificar para mandarlo por acá. Descargalo desde el panel.")
                    else:
                        res = row.get("resultado") or {}
                        with open(path, "rb") as stream:
                            await bot.send_document(chat_id=chat, document=stream, filename=path.name,
                                caption=f"{res.get('filas', 0)} filas · también en Documentos")

    async def run(self, adapter) -> None:
        while True:
            try:
                if adapter._bot is not None:
                    await self.poll(adapter._bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[COMARB-DEDUCCIONES] entrega: diagnóstico técnico no disponible")
            await asyncio.sleep(5)
