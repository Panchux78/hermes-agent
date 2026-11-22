"""Cruce de varios meses con Libros IVA por Telegram (Ágora #152).

Un cliente, un rango de hasta 12 meses y sus extractos (los Excel ya generados de ese rango y,
si hace falta, PDF de resúmenes). El pedido lo lleva el orquestador del worker fiscal de ContaBot;
acá sólo se llama a ``console.fn_cruce_iva_meses_telegram`` con el rol restringido del gateway.

Lo que no puede perderse con un reinicio del bot vive en la base y lo atiende ``run``: el CAPTCHA
del lote de Libros IVA hijo y la entrega del resultado, una sola vez. El mensaje de avance es sólo
una comodidad de esta sesión. No se toca «Cruce con Libros IVA» (cx:start).
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import logging
import re
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import CONTRIBUTOR_PROMPT, MULTIPLE_CONTRIBUTORS_TEXT, ContributorOffer
from plugins.platforms.telegram.constancias_flow import ConstanciasFlow
from plugins.platforms.telegram.cruce_iva_flow import CruceIvaFlow
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_scope import validate_search_term
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow

logger = logging.getLogger(__name__)

TITLE = "Cruce de varios meses con Libros IVA"
START = "cx:meses"
PREFIX = "cxm"
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDFS = 24
ACTIVE = {"pendiente", "convirtiendo", "bajando", "cruzando"}
STAGING_PDF = re.compile(r"telegram/[0-9]+/[0-9a-f]{32}\.pdf")
RANGE = re.compile(r"^\s*(0[1-9]|1[0-2])/(20\d{2})\s*(?:a|-|al|hasta)\s*(0[1-9]|1[0-2])/(20\d{2})\s*$", re.I)


@dataclass
class State:
    nonce: str
    user_id: str
    created_at: float
    stage: str = "search"  # search → range → plan → pdf → (pedido)
    contributor_id: int | None = None
    contributor_name: str | None = None
    candidates: dict | None = None
    desde: str = ""
    hasta: str = ""
    plan: dict | None = None
    pdfs: list[dict] = field(default_factory=list)


def plural(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def meses_rango(desde: str, hasta: str) -> int:
    md, ad = map(int, desde.split("/"))
    mh, ah = map(int, hasta.split("/"))
    return (ah - ad) * 12 + mh - md + 1


def plan_text(state: State) -> str:
    plan = state.plan or {}
    planillas = plan.get("planillas") or []
    meses = plan.get("meses") or []
    bajar = [m["periodo"] for m in meses if m.get("bajar")]
    en_docs = sum(1 for m in meses if m.get("rol") != "sin_extractos" and not m.get("bajar"))
    sin = [m["periodo"] for m in meses if m.get("rol") == "sin_extractos"]
    lines = [f"{TITLE} · {state.contributor_name} · {state.desde} a {state.hasta}",
             f"Extractos: {plural(len(planillas), 'Excel ya generado', 'Excel ya generados')}"
             + (f" y {plural(len(state.pdfs), 'PDF para convertir', 'PDF para convertir')}" if state.pdfs else "") + "."]
    if sin:
        lines.append("Sin extractos (no se cruzan): " + ", ".join(sin) + ". Si los tenés, mandá los PDF.")
    lines.append(f"Libros IVA: {plural(en_docs, 'mes ya está', 'meses ya están')} en Documentos.")
    if bajar and plan.get("elegible"):
        lines.append("Se bajan de Portal IVA: " + ", ".join(bajar) + ".")
    elif bajar:
        lines.append(f"Faltan {', '.join(bajar)} y no se pueden bajar: el cliente {plan.get('motivo_acceso') or 'no está habilitado'}. "
                     "Se cruza con lo que hay.")
    lines.append("El cruce también usa los libros de los 3 meses anteriores a cada movimiento. Sólo se consulta: no se presenta nada.")
    return "\n".join(lines)


def result_text(row: dict) -> str:
    """Aviso final: el mismo detalle que el panel, sin datos de otros clientes."""
    head = f"{TITLE} · {row.get('contribuyente')} · {row.get('desde')} a {row.get('hasta')}"
    r = row.get("resultado") or {}
    estado = row.get("estado")
    if estado in ("completo", "parcial"):
        lines = [head, f"{CruceIvaFlow.summary(r)}."]
        meses = row.get("meses") or []
        bajados = sum(1 for m in meses if (m.get("ventas") or {}).get("estado") == "bajado"
                      or (m.get("compras") or {}).get("estado") == "bajado")
        if bajados:
            lines.append(f"Se bajaron de Portal IVA {plural(bajados, 'mes', 'meses')}; quedaron en Documentos.")
        if r.get("pendiente"):
            lines.append("Sin cruzar completo: " + str(r["pendiente"]))
            lines.append("Cuando esté el libro, tocá «Reintentar los meses que faltan».")
        if r.get("sin_acceso"):
            lines.append(str(r["sin_acceso"]))
        lines.append("También queda en Documentos y Operaciones.")
        return "\n".join(lines)
    return head + "\n" + str(row.get("error") or row.get("progreso") or "El pedido terminó.")


class CruceIvaMesesFlow:
    TTL = 20 * 60
    POLL_SECONDS = 3.0
    WATCH_SECONDS = 2 * 60 * 60

    def __init__(self, *, staging_root=None, clientes_root=None) -> None:
        helper = CruceIvaFlow(staging_root=staging_root, clientes_root=clientes_root)
        self.staging_root, self.clientes_root = helper.staging_root, helper.clientes_root
        self._staging_dir = helper._staging_dir
        self.states: dict[str, State] = {}
        self.watchers: dict[int, asyncio.Task] = {}
        self.captchas: dict[tuple[int, int], tuple[int, str]] = {}  # (chat, mensaje) → (pedido, nonce)
        self.captchas_enviados: set[str] = set()

    # ----------------------------------------------------------------- base
    _key = staticmethod(CruceIvaFlow._key)
    _literal = staticmethod(ConstanciasFlow._literal)
    _number = staticmethod(ConstanciasFlow._number)

    @classmethod
    def _query(cls, sql: str) -> list[dict]:
        return ConstanciasFlow._query(sql)

    @classmethod
    def _call(cls, actor: int, accion: str, jid: int | None = None, cid: int | None = None, datos: dict | None = None):
        sql = ("SELECT console.fn_cruce_iva_meses_telegram("
               f"{int(actor)},{cls._literal(accion)},{int(jid) if jid else 'NULL'},{int(cid) if cid else 'NULL'},"
               f"{cls._literal(json.dumps(datos or {}, ensure_ascii=False))}::jsonb)::text;")
        rows = cls._query(sql)
        if len(rows) > 1:
            raise RuntimeError("cruce_iva_meses_respuesta_ambigua")
        return rows[0] if rows and isinstance(rows[0], dict) else None

    @staticmethod
    def _cancel(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"{PREFIX}:cancel:{nonce}")]])

    @staticmethod
    def _plan_keyboard(state: State) -> InlineKeyboardMarkup:
        rows = []
        if (state.plan or {}).get("planillas") or state.pdfs:
            rows.append([InlineKeyboardButton(menu_label("🔀", "Cruzar"), callback_data=f"{PREFIX}:go:{state.nonce}")])
        rows.append([InlineKeyboardButton(menu_label("📤", "Agregar PDF de resúmenes"), callback_data=f"{PREFIX}:pdf:{state.nonce}")])
        rows.append([InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"{PREFIX}:cancel:{state.nonce}")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _stop(jid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar el pedido"), callback_data=f"{PREFIX}:stop:{jid}")]])

    @staticmethod
    def _retry(jid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("🔁", "Reintentar los meses que faltan"),
                                                           callback_data=f"{PREFIX}:retry:{jid}")]])

    def cancel_pending(self, chat_id, thread_id, user_id) -> bool:
        return self.states.pop(self._key(chat_id, thread_id, user_id), None) is not None

    # ------------------------------------------------------------ callbacks
    def handles(self, data: str) -> bool:
        return data == START or data.startswith(PREFIX + ":")

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id: str) -> bool:
        if not self.handles(data):
            return False
        if str(chat_id) != str(user_id):
            await query.answer("Este cruce se pide por privado.")
            return True
        key = self._key(chat_id, thread_id, user_id)
        parts = data.split(":")
        if data == START:
            try:
                activo = await asyncio.to_thread(self._call, int(user_id), "activo")
            except Exception:
                activo = None
            await query.answer()
            if activo and activo.get("id_cruce_meses"):
                await self._show_active(adapter, query.message, int(user_id), int(activo["id_cruce_meses"]))
                return True
            state = State(uuid.uuid4().hex[:10], str(user_id), time.monotonic())
            self.states[key] = state
            await query.edit_message_text(f"{TITLE}\n{CONTRIBUTOR_PROMPT}", reply_markup=self._cancel(state.nonce))
            return True
        if len(parts) == 3 and parts[1] in ("stop", "retry") and parts[2].isdigit():
            return await self._order_action(adapter, query, int(user_id), parts[1], int(parts[2]))
        state = self.states.get(key)
        if not state or len(parts) < 3 or parts[2] != state.nonce or time.monotonic() - state.created_at > self.TTL:
            await query.answer(f"Este pedido venció. Iniciá uno nuevo desde Bancos › {TITLE}.")
            return True
        state.created_at = time.monotonic()
        action = parts[1]
        if action == "cancel":
            self._drop(key)
            await query.answer()
            await query.edit_message_text(f"{TITLE}: cancelado. No se pidió nada.")
            return True
        if action == "select" and state.stage == "search" and len(parts) == 4:
            candidate = (state.candidates or {}).get(self._number(parts[3]))
            if not candidate:
                await query.answer("Esta opción ya no está disponible.")
                return True
            await query.answer()
            self._chosen(state, candidate)
            await query.edit_message_text(self._range_prompt(state), reply_markup=self._cancel(state.nonce))
            return True
        if action == "pdf" and state.stage in ("plan", "pdf"):
            state.stage = "pdf"
            await query.answer()
            await query.edit_message_text(
                f"{TITLE} · {state.contributor_name}\nMandame los PDF de los resúmenes de este cliente como documentos "
                f"(hasta {MAX_PDFS}, de 5 MB cada uno). Se convierten como en «Resumen bancario → Excel»; uno de otro "
                "cliente se informa y no se usa. Cuando termines, tocá «Cruzar».", reply_markup=self._plan_keyboard(state))
            return True
        if action == "go" and state.stage in ("plan", "pdf"):
            await query.answer()
            await self._submit(adapter, query.message, state, key)
            return True
        await query.answer("Esta opción ya no está disponible.")
        return True

    def _chosen(self, state: State, candidate: dict) -> None:
        state.contributor_id = self._number(candidate["id"])
        state.contributor_name = str(candidate["nombre"])
        state.stage = "range"

    def _range_prompt(self, state: State) -> str:
        return (f"{TITLE} · {state.contributor_name}\nMandame el rango de meses, hasta 12. "
                "Por ejemplo: 11/2025 a 10/2026.")

    async def _order_action(self, adapter, query, actor: int, action: str, jid: int) -> bool:
        try:
            row = await asyncio.to_thread(self._call, actor, "cancelar" if action == "stop" else "reintentar", jid)
        except Exception:
            row = None
        if action == "stop":
            await query.answer("Cancelación pedida. Lo ya bajado queda en Documentos." if row and row.get("ok")
                               else "Ya no se puede cancelar: el cruce empezó o el pedido terminó.")
            return True
        if not row or not row.get("id_cruce_meses"):
            await query.answer("No se pudo reintentar. Revisá el pedido en el panel.")
            return True
        await query.answer()
        message = await query.message.reply_text(f"{TITLE}\nReintentando los meses que faltan…",
                                                 reply_markup=self._stop(int(row["id_cruce_meses"])))
        self._watch(message, actor, int(row["id_cruce_meses"]))
        return True

    # ---------------------------------------------------------------- texto
    async def text(self, adapter, message) -> bool:
        if await self._captcha_reply(message):
            return True
        user = getattr(message, "from_user", None)
        key = self._key(message.chat_id, getattr(message, "message_thread_id", None), getattr(user, "id", ""))
        state = self.states.get(key)
        if not state or state.stage not in ("search", "range", "pdf"):
            return False
        if time.monotonic() - state.created_at > self.TTL:
            self._drop(key)
            return False
        state.created_at = time.monotonic()
        if state.stage == "pdf":
            await message.reply_text("Esperaba los PDF como documentos. Mandalos adjuntos o tocá «Cruzar».",
                                     reply_markup=self._plan_keyboard(state))
            return True
        if state.stage == "search":
            try:
                rows = await asyncio.to_thread(CruceIvaFlow._search, int(state.user_id), message.text or "")
                offer = ContributorOffer.from_rows(rows)
            except Exception:
                await message.reply_text("No pude buscar el cliente. Volvé a intentarlo en unos minutos.",
                                         reply_markup=self._cancel(state.nonce))
                return True
            if offer.status == "empty":
                await message.reply_text("No encontré un cliente autorizado con ese nombre, CUIT o alias.",
                                         reply_markup=self._cancel(state.nonce))
            elif offer.status == "single":
                self._chosen(state, offer.single)
                await message.reply_text(self._range_prompt(state), reply_markup=self._cancel(state.nonce))
            else:
                state.candidates = offer.candidates
                await message.reply_text(MULTIPLE_CONTRIBUTORS_TEXT, reply_markup=offer.keyboard(
                    callback_prefix=PREFIX, nonce=state.nonce, cancel_text=menu_label("✕", "Cancelar")))
            return True
        match = RANGE.match(message.text or "")
        if not match:
            await message.reply_text("Mandame el rango así: 11/2025 a 10/2026.", reply_markup=self._cancel(state.nonce))
            return True
        desde, hasta = f"{match[1]}/{match[2]}", f"{match[3]}/{match[4]}"
        if not 1 <= meses_rango(desde, hasta) <= 12:
            await message.reply_text("El rango tiene que ser de 1 a 12 meses. Mandalo de nuevo.", reply_markup=self._cancel(state.nonce))
            return True
        try:
            plan = await asyncio.to_thread(self._call, int(state.user_id), "preparar", None, state.contributor_id,
                                           {"desde": desde, "hasta": hasta})
        except Exception as exc:
            logger.warning("cruce_iva_meses_preparar_failed error=%s", type(exc).__name__)
            plan = None
        if not plan:
            await message.reply_text("No pude revisar ese rango (tiene que terminar a más tardar en el mes en curso). "
                                     "Mandalo de nuevo.", reply_markup=self._cancel(state.nonce))
            return True
        if plan.get("activo"):
            self._drop(key)
            await message.reply_text(f"Ya hay un {TITLE.lower()} en curso para este cliente. Esperá a que termine.")
            return True
        state.desde, state.hasta, state.plan, state.stage = desde, hasta, plan, "plan"
        await message.reply_text(plan_text(state), reply_markup=self._plan_keyboard(state))
        return True

    # -------------------------------------------------------------- PDF
    async def document(self, adapter, message) -> bool:
        user = getattr(message, "from_user", None)
        key = self._key(message.chat_id, getattr(message, "message_thread_id", None), getattr(user, "id", ""))
        state = self.states.get(key)
        if not state or state.stage != "pdf":
            return False
        state.created_at = time.monotonic()
        document = getattr(message, "document", None)
        name = str(getattr(document, "file_name", "") or "")
        size = int(getattr(document, "file_size", 0) or 0)
        if not document or not name.lower().endswith(".pdf") or not 0 < size <= MAX_PDF_BYTES:
            await message.reply_text("Esperaba un PDF de hasta 5 MB. Mandalo de nuevo como documento.",
                                     reply_markup=self._plan_keyboard(state))
            return True
        if len(state.pdfs) >= MAX_PDFS:
            await message.reply_text(f"Ya hay {MAX_PDFS} PDF, el máximo. Tocá «Cruzar».", reply_markup=self._plan_keyboard(state))
            return True
        try:
            data = bytes(await (await document.get_file()).download_as_bytearray())
            if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
                raise ValueError("pdf_invalido")
            import hashlib
            sha = hashlib.sha256(data).hexdigest()
            if any(p["sha256"] == sha for p in state.pdfs):
                await message.reply_text("Ese PDF ya estaba: no se agrega dos veces.", reply_markup=self._plan_keyboard(state))
                return True
            staging = await asyncio.to_thread(self._write_pdf, int(state.user_id), data)
        except Exception as exc:
            logger.warning("cruce_iva_meses_pdf_failed error=%s", type(exc).__name__)
            await message.reply_text("No pude recibir ese PDF. Mandalo de nuevo.", reply_markup=self._plan_keyboard(state))
            return True
        nombre = CruceIvaFlow._upload_name(name)
        nombre = nombre if nombre.lower().endswith(".pdf") and len(nombre) >= 5 else "resumen.pdf"
        state.pdfs.append({"nombre": nombre, "staging": staging, "sha256": sha, "bytes": len(data)})
        await message.reply_text(f"Recibí {plural(len(state.pdfs), 'PDF', 'PDF')}. Mandá más o tocá «Cruzar».",
                                 reply_markup=self._plan_keyboard(state))
        return True

    def _write_pdf(self, telegram_id: int, data: bytes) -> str:
        import os
        directory = self._staging_dir(telegram_id)
        relative = f"telegram/{int(telegram_id)}/{uuid.uuid4().hex}.pdf"
        target = self.staging_root / relative
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if target.parent != directory or not STAGING_PDF.fullmatch(relative):
            target.unlink(missing_ok=True)
            raise RuntimeError("cruce_iva_meses_staging_invalido")
        return relative

    def _drop(self, key: str) -> None:
        state = self.states.pop(key, None)
        for pdf in (state.pdfs if state else []):
            if STAGING_PDF.fullmatch(pdf["staging"]):
                (self.staging_root / pdf["staging"]).unlink(missing_ok=True)

    # ------------------------------------------------------------ pedido
    async def _submit(self, adapter, message, state: State, key: str) -> None:
        datos = {"desde": state.desde, "hasta": state.hasta,
                 "planillas": [p["ruta_relativa"] for p in (state.plan or {}).get("planillas") or []], "pdfs": state.pdfs}
        try:
            row = await asyncio.to_thread(self._call, int(state.user_id), "pedir", None, state.contributor_id, datos)
        except Exception as exc:
            logger.warning("cruce_iva_meses_pedir_failed error=%s", type(exc).__name__)
            row = None
        if not row or not row.get("id_cruce_meses"):
            await message.reply_text("No se pudo confirmar el pedido. Si ya hay uno en curso para este cliente, esperá a que "
                                     f"termine; si no, volvé a pedirlo desde Bancos › {TITLE}.")
            return
        self.states.pop(key, None)  # los PDF ya son del pedido: los lee el orquestador
        jid = int(row["id_cruce_meses"])
        progress = await message.reply_text(f"{TITLE} · {state.contributor_name} · {state.desde} a {state.hasta}\n"
                                            "Pedido confirmado. Podés seguir usando el bot: te aviso al terminar.",
                                            reply_markup=self._stop(jid))
        self._watch(progress, int(state.user_id), jid)

    async def _show_active(self, adapter, message, actor: int, jid: int) -> None:
        try:
            row = await asyncio.to_thread(self._call, actor, "estado", jid)
        except Exception:
            row = None
        if not row:
            await message.reply_text("No pude leer tu pedido en curso. Revisalo en el panel o en Operaciones.")
            return
        sent = await message.reply_text(f"{TITLE} · {row['contribuyente']} · {row['desde']} a {row['hasta']}\n{row['progreso']}",
                                        reply_markup=self._stop(jid) if row.get("cancelable") else None)
        self._watch(sent, actor, jid)

    def _watch(self, message, actor: int, jid: int) -> None:
        if jid not in self.watchers:
            self.watchers[jid] = asyncio.create_task(self._watch_loop(message, actor, jid))

    async def _watch_loop(self, message, actor: int, jid: int) -> None:
        last = ""
        deadline = time.monotonic() + self.WATCH_SECONDS
        try:
            while time.monotonic() < deadline:
                row = await asyncio.to_thread(self._call, actor, "estado", jid)
                if not row:
                    return
                head = f"{TITLE} · {row['contribuyente']} · {row['desde']} a {row['hasta']}"
                activo = row["estado"] in ACTIVE
                text = head + "\n" + (row["progreso"] if activo else "Terminó. Te mando el resultado.")
                if text != last:
                    await message.edit_text(text, reply_markup=self._stop(jid) if activo and row.get("cancelable") else None)
                    last = text
                if not activo:
                    return
                await asyncio.sleep(self.POLL_SECONDS)
        except Exception as exc:
            logger.warning("cruce_iva_meses_progreso_no_disponible pedido=%s clase=%s", jid, type(exc).__name__)
        finally:
            self.watchers.pop(jid, None)

    # ------------------------------------------------------------ CAPTCHA
    async def _send_captchas(self, adapter) -> None:
        rows = await asyncio.to_thread(self._query,
                                       "SELECT row_to_json(x)::text FROM console.fn_cruce_iva_meses_captchas_telegram() x;")
        for row in rows:
            nonce = str(row.get("nonce") or "")
            if not re.fullmatch(r"[0-9a-f]{20,64}", nonce) or nonce in self.captchas_enviados:
                continue
            actor, jid = int(row["telegram_id"]), int(row["id_cruce_meses"])
            image = await asyncio.to_thread(self._call, actor, "captcha_imagen", jid)
            if not image or not image.get("imagen"):
                continue
            from telegram import ForceReply
            self.captchas_enviados.add(nonce)
            sent = await adapter._bot.send_photo(
                chat_id=actor, photo=base64.b64decode(image["imagen"]),
                caption=f"{TITLE}. ARCA pide este CAPTCHA para bajar los Libros IVA que faltan. Respondé a esta imagen "
                        "con sus caracteres; vence en menos de dos minutos. Si vence, esos meses quedan sin bajar y se cruza con los demás.",
                reply_markup=ForceReply(selective=True))
            self.captchas[(actor, int(sent.message_id))] = (jid, nonce)

    async def _captcha_reply(self, message) -> bool:
        reply = getattr(message, "reply_to_message", None)
        if reply is None:
            return False
        pending = self.captchas.get((int(message.chat_id), int(getattr(reply, "message_id", 0) or 0)))
        if pending is None:
            return False
        jid, nonce = pending
        solucion = (message.text or "").strip()
        ok = False
        if re.fullmatch(r"[A-Za-z0-9]{4,20}", solucion):
            try:
                row = await asyncio.to_thread(self._call, int(message.from_user.id), "captcha", jid, None,
                                              {"nonce": nonce, "solucion": solucion})
                ok = bool(row and row.get("ok"))
            except Exception:
                ok = False
        from plugins.platforms.telegram.panel_captcha_bridge import teclado_menu
        if ok:
            self.captchas.pop((int(message.chat_id), int(reply.message_id)), None)
            await message.reply_text("Recibido. Sigo bajando los Libros IVA.", reply_markup=teclado_menu())
        else:
            await message.reply_text("No pude usar ese texto: venció o no son los caracteres de la imagen. "
                                     "Si ARCA pide otro, te lo mando.", reply_markup=teclado_menu())
        return True

    # ------------------------------------------------------------ entrega
    async def _deliver(self, adapter) -> None:
        rows = await asyncio.to_thread(self._query, "SELECT row_to_json(x)::text FROM console.fn_cruce_iva_meses_entregas() x;")
        for pending in rows:
            actor, jid = int(pending["telegram_id"]), int(pending["id_cruce_meses"])
            row = await asyncio.to_thread(self._call, actor, "reclamar", jid)
            if not row:
                continue
            message_id = None
            try:
                markup = self._retry(jid) if row.get("reintentable") else None
                texto = result_text(row)
                if row.get("estado") in ("completo", "parcial"):
                    a = row.get("archivo")
                    if not a:
                        raise ValueError("archivo_no_disponible")
                    path = archivo_guardado(None, self.clientes_root, a["ruta"], str(a["sha256"]).strip())
                    fresh = await asyncio.to_thread(self._call, actor, "estado", jid)
                    if not fresh or fresh.get("archivo") != a:
                        raise ValueError("autorizacion_cambio")
                    delivery = await adapter.send_document(chat_id=str(actor), file_path=str(path),
                                                           file_name=PdfXlsxFlow._delivery_filename(path),
                                                           caption=CruceIvaFlow.summary(row.get("resultado") or {}))
                    if not getattr(delivery, "success", False):
                        raise RuntimeError("telegram_no_confirmo")
                    message_id = getattr(delivery, "message_id", None)
                sent = await adapter._bot.send_message(chat_id=actor, text=texto, reply_markup=markup)
                message_id = message_id or sent.message_id
                value = str(int(message_id)) if message_id is not None else "NULL"
                await asyncio.to_thread(self._query, f"SELECT console.fn_cruce_iva_meses_entrega({actor},{jid},'enviada',{value})::text;")
            except Exception as exc:
                logger.warning("cruce_iva_meses_entrega_fallo pedido=%s clase=%s", jid, type(exc).__name__)
                await asyncio.to_thread(self._query, f"SELECT console.fn_cruce_iva_meses_entrega({actor},{jid},'error')::text;")
                await adapter._bot.send_message(chat_id=actor, text=f"{TITLE} terminó, pero no pude confirmar el envío. "
                                                                    "Descargá el Excel desde el panel (Bancos o Documentos).")

    async def poll(self, adapter) -> None:
        await self._send_captchas(adapter)
        await self._deliver(adapter)

    async def run(self, adapter) -> None:
        try:
            await asyncio.to_thread(self._query, "SELECT console.fn_cruce_iva_meses_entregas_recuperar()::text;")
        except Exception as exc:
            logger.warning("cruce_iva_meses_recuperar_no_disponible clase=%s", type(exc).__name__)
        while True:
            try:
                await self.poll(adapter)
            except Exception as exc:
                logger.warning("cruce_iva_meses_entregador_no_disponible clase=%s", type(exc).__name__)
            await asyncio.sleep(self.POLL_SECONDS)
