"""Cruce de planillas bancarias con los Libros IVA por Telegram (Ágora #122).

El gateway no toca la tabla de pedidos: sólo llama a las funciones
``console.fn_cruce_iva_telegram_*`` con el rol restringido del gateway (el mismo
transporte que Constancias). El cruce lo hace el worker fiscal; acá se elige el
cliente y las planillas (o se sube un Excel), se encola y se entrega el resultado.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import stat
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import (
    CONTRIBUTOR_PROMPT,
    MULTIPLE_CONTRIBUTORS_TEXT,
    ContributorOffer,
)
from plugins.platforms.telegram.constancias_flow import ConstanciasFlow
from plugins.platforms.telegram.fiscal_scope import validate_search_term
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
STAGING_PATTERN = re.compile(r"telegram/[0-9]+/[0-9a-f]{32}\.xlsx")
TITLE = "Cruce con Libros IVA"
PROGRESS_TEXT = "Cruzando con Libros IVA…"
TERMINAL_STATES = {"terminado", "formato_invalido", "sin_libros", "fallido"}
ACTIVE_STAGES = {"running"}


def _default_staging_root() -> Path:
    return Path(os.getenv("CONTABOT_CRUCE_IVA_STAGING_ROOT",
                          str(Path.home() / ".local/state/contabot/cruce-iva")))


def _default_clientes_root() -> Path:
    return Path(os.getenv("CONTABOT_CLIENTES_ROOT", str(Path.home() / "clientes")))


@dataclass
class State:
    nonce: str
    user_id: str
    created_at: float
    stage: str = "search"
    contributor_id: int | None = None
    contributor_name: str | None = None
    candidates: dict | None = None
    planillas: list[dict] = field(default_factory=list)
    selected: set[int] = field(default_factory=set)
    cruce_id: int | None = None
    progress_message: object | None = None
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class ConversionOffer:
    """Planilla recién entregada por «Resumen bancario → Excel»."""

    user_id: str
    chat_id: str
    thread_id: object
    contributor_id: int
    ruta_relativa: str
    created_at: float


class CruceIvaFlow:
    TTL = 15 * 60
    OFFER_TTL = 6 * 60 * 60
    MAX_OFFERS = 200
    POLL_SECONDS = 3.0
    POLL_TIMEOUT_SECONDS = 10 * 60

    def __init__(self, *, staging_root: Path | None = None, clientes_root: Path | None = None) -> None:
        self.staging_root = Path(staging_root) if staging_root else _default_staging_root()
        self.clientes_root = Path(clientes_root) if clientes_root else _default_clientes_root()
        self.states: dict[str, State] = {}
        self.offers: dict[str, ConversionOffer] = {}

    # ----------------------------------------------------------------- base
    @staticmethod
    def _key(chat_id, thread_id, user_id) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    _literal = staticmethod(ConstanciasFlow._literal)
    _number = staticmethod(ConstanciasFlow._number)

    @classmethod
    def _query(cls, sql: str) -> list[dict]:
        # Mismo transporte DB restringido que Constancias.
        return ConstanciasFlow._query(sql)

    @classmethod
    def _search(cls, telegram_id: int, term: str) -> list[dict]:
        return cls._query(
            "SELECT json_build_object('id',id_contribuyente,'nombre',nombre_legal,"
            "'slug',slug,'cuit',cuit,'study_id',id_estudio)::text FROM console.fn_constancia_telegram_buscar("
            f"{int(telegram_id)},{cls._literal(validate_search_term(term))});"
        )

    @classmethod
    def _planillas(cls, telegram_id: int, contributor_id: int) -> list[dict]:
        rows = cls._query(
            "SELECT json_build_object('ruta',ruta_relativa,'banco',banco,'origen',origen,'periodo',periodo)::text "
            f"FROM console.fn_cruce_iva_telegram_planillas({int(telegram_id)},{int(contributor_id)});"
        )
        if len(rows) > 60 or not all(isinstance(row, dict) and isinstance(row.get("ruta"), str) for row in rows):
            raise RuntimeError("cruce_iva_planillas_invalidas")
        return rows

    @classmethod
    def _begin(cls, telegram_id: int, contributor_id: int, *, planillas: list[str] | None = None,
               archivo_nombre: str | None = None, archivo_staging: str | None = None) -> int:
        planillas = list(planillas or [])
        if bool(planillas) == bool(archivo_staging) or bool(archivo_nombre) != bool(archivo_staging):
            raise ValueError("cruce_iva_origen_invalido")
        if archivo_staging is not None and not STAGING_PATTERN.fullmatch(archivo_staging):
            raise ValueError("cruce_iva_staging_invalido")
        array = ("ARRAY[" + ",".join(cls._literal(item) for item in planillas) + "]::text[]"
                 if planillas else "'{}'::text[]")
        name = cls._literal(archivo_nombre) if archivo_nombre else "NULL::text"
        staging = cls._literal(archivo_staging) if archivo_staging else "NULL::text"
        rows = cls._query(
            "SELECT json_build_object('id_cruce',console.fn_cruce_iva_telegram_iniciar("
            f"{int(telegram_id)},{int(contributor_id)},{array},{name},{staging}))::text;"
        )
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("cruce_iva_respuesta_ambigua")
        return cls._number(rows[0]["id_cruce"])

    @classmethod
    def _status(cls, telegram_id: int, cruce_id: int) -> dict:
        rows = cls._query(
            f"SELECT console.fn_cruce_iva_telegram_estado({int(telegram_id)},{int(cruce_id)})::text;"
        )
        if len(rows) != 1 or not isinstance(rows[0], dict) or not isinstance(rows[0].get("estado"), str):
            raise RuntimeError("cruce_iva_estado_ilegible")
        return rows[0]

    # ------------------------------------------------------------ teclados
    @staticmethod
    def _cancel_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"cx:cancel:{nonce}")]])

    @staticmethod
    def _source_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(menu_label("📂", "Elegir Excel ya generados"), callback_data=f"cx:pick:{nonce}")],
            [InlineKeyboardButton(menu_label("📤", "Subir un Excel"), callback_data=f"cx:up:{nonce}")],
            [InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"cx:cancel:{nonce}")],
        ])

    @staticmethod
    def _upload_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(menu_label("📤", "Subir un Excel"), callback_data=f"cx:up:{nonce}")],
            [InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"cx:cancel:{nonce}")],
        ])

    @staticmethod
    def _short(text: str, limit: int) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    @classmethod
    def planilla_label(cls, row: dict, selected: bool) -> str:
        bank = cls._short(row.get("banco") or "Banco", 22)
        period = str(row.get("periodo") or "")
        month = f"{period[5:7]}/{period[:4]}" if re.fullmatch(r"[0-9]{4}-[0-9]{2}", period) else "sin mes"
        source = str(row.get("origen") or "")
        source = source[:-4] if source.lower().endswith(".pdf") else source
        return menu_label("✅" if selected else "⬜", f"{bank} · {month} · {cls._short(source or 'sin PDF', 24)}")

    @classmethod
    def _pick_keyboard(cls, state: State) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(cls.planilla_label(row, index in state.selected),
                                  callback_data=f"cx:t:{state.nonce}:{index}")]
            for index, row in enumerate(state.planillas)
        ]
        rows.append([InlineKeyboardButton(menu_label("🔀", "Cruzar seleccionados"), callback_data=f"cx:go:{state.nonce}")])
        rows.append([InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"cx:cancel:{state.nonce}")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _client(state: State) -> str:
        return state.contributor_name or "este cliente"

    def _pick_text(self, state: State) -> str:
        count = len(state.selected)
        chosen = "ninguno elegido" if count == 0 else ("1 elegido" if count == 1 else f"{count} elegidos")
        return (f"{TITLE} · {self._client(state)}\n"
                f"Tocá los Excel que querés cruzar y después «Cruzar seleccionados» ({chosen}).")

    def _source_text(self, state: State, prefix: str = "") -> str:
        return f"{prefix}{TITLE} · {self._client(state)}\n¿Con qué Excel querés cruzar?"

    # --------------------------------------------------------------- textos
    @staticmethod
    def _count(value) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def summary(cls, result: dict) -> str:
        return (f"{cls._count(result.get('movimientos'))} movimientos · "
                f"{cls._count(result.get('deudores'))} deudores · "
                f"{cls._count(result.get('proveedores'))} proveedores · "
                f"{cls._count(result.get('en_blanco'))} en blanco")

    @staticmethod
    def missing_books_text(result: dict) -> str | None:
        missing = [str(item) for item in (result.get("libros_faltantes") or [])
                   if re.fullmatch(r"[0-9]{4}-[0-9]{2}", str(item))]
        if not missing:
            return None
        months = ", ".join(f"{item[5:7]}/{item[:4]}" for item in missing)
        return f"Faltan los Libros IVA de {months}: bajalos con ARCA › Consultar › Lote de Libros IVA."

    @staticmethod
    def failure_text(status: dict) -> str:
        state = status.get("estado")
        error = str(status.get("error") or "").strip()
        if state == "formato_invalido":
            result = status.get("resultado") if isinstance(status.get("resultado"), dict) else {}
            problems = [str(item) for item in (result.get("problemas") or []) if str(item).strip()]
            lines = [error or "El Excel no tiene el formato esperado para el cruce."]
            lines += [f"• {item}" for item in problems]
            return "\n".join(lines)
        if state == "sin_libros":
            return error or "No encontré Libros IVA de ese cliente para los meses de los movimientos."
        return error or "No pude hacer el cruce. Volvé a intentarlo en unos minutos."

    # -------------------------------------------------------------- salida
    def _output_path(self, relative) -> Path:
        if not isinstance(relative, str) or not relative or relative.startswith("/") or "\x00" in relative:
            raise RuntimeError("cruce_iva_salida_invalida")
        if any(part in {"", ".", ".."} for part in relative.split("/")):
            raise RuntimeError("cruce_iva_salida_invalida")
        root = self.clientes_root.resolve(strict=True)
        candidate = self.clientes_root / relative
        current = self.clientes_root
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                raise RuntimeError("cruce_iva_salida_enlace")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or candidate.suffix.lower() != ".xlsx":
            raise RuntimeError("cruce_iva_salida_invalida")
        return candidate

    # -------------------------------------------------------------- staging
    def _staging_dir(self, telegram_id: int) -> Path:
        for directory in (self.staging_root, self.staging_root / "telegram",
                          self.staging_root / "telegram" / str(int(telegram_id))):
            if directory.is_symlink():
                raise RuntimeError("cruce_iva_staging_enlace")
            if not directory.exists():
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
            elif not directory.is_dir():
                raise RuntimeError("cruce_iva_staging_invalido")
        return self.staging_root / "telegram" / str(int(telegram_id))

    def _write_staging(self, telegram_id: int, data: bytes) -> str:
        directory = self._staging_dir(telegram_id)
        relative = f"telegram/{int(telegram_id)}/{uuid.uuid4().hex}.xlsx"
        target = self.staging_root / relative
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        target.chmod(0o600)
        if target.parent != directory or not STAGING_PATTERN.fullmatch(relative):
            raise RuntimeError("cruce_iva_staging_invalido")
        return relative

    @staticmethod
    def _upload_name(raw: str) -> str:
        name = Path(str(raw or "").replace("\\", "/")).name.replace("/", "_").strip() or "planilla.xlsx"
        if len(name) > 200:
            name = name[: 200 - len(".xlsx")].rstrip() + ".xlsx"
        return name

    # ----------------------------------------------------------------- API
    def cancel_pending(self, chat_id, thread_id, user_id) -> bool:
        """Descarta un pedido sin encolar: el usuario eligió otra opción del menú."""
        key = self._key(chat_id, thread_id, user_id)
        state = self.states.get(key)
        if state is None or state.stage in ACTIVE_STAGES:
            return False
        self.states.pop(key, None)
        return True

    def register_offer(self, *, user_id, chat_id, thread_id, contributor_id, ruta_relativa):
        """Botón «Cruzar con Libros IVA» para la planilla recién entregada, o None."""
        if (not isinstance(contributor_id, int) or isinstance(contributor_id, bool) or contributor_id <= 0
                or not isinstance(ruta_relativa, str) or not ruta_relativa.lower().endswith(".xlsx")
                or ruta_relativa.startswith("/") or ".." in ruta_relativa.split("/")):
            return None
        now = time.monotonic()
        for token, offer in list(self.offers.items()):
            if now - offer.created_at > self.OFFER_TTL:
                self.offers.pop(token, None)
        while len(self.offers) >= self.MAX_OFFERS:
            self.offers.pop(next(iter(self.offers)))
        token = uuid.uuid4().hex[:16]
        self.offers[token] = ConversionOffer(str(user_id), str(chat_id), thread_id, contributor_id,
                                             ruta_relativa, now)
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("🔀", "Cruzar con Libros IVA"),
                                                           callback_data=f"cx:of:{token}")]])

    async def _edit(self, state: State, text: str, reply_markup=None) -> None:
        try:
            await state.progress_message.edit_text(text, reply_markup=reply_markup)
        except Exception as exc:
            logger.warning("cruce_iva_edit_failed stage=%s error=%s", state.stage, type(exc).__name__)

    async def _notify(self, adapter, state: State, chat_id, thread_id, text: str) -> None:
        """Edita el mensaje de progreso; si no se puede, manda uno nuevo."""
        try:
            await state.progress_message.edit_text(text, reply_markup=None)
            return
        except Exception as exc:
            logger.warning("cruce_iva_progress_edit_failed error=%s", type(exc).__name__)
        kwargs = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            await adapter._bot.send_message(**kwargs)
        except Exception as exc:
            logger.error("cruce_iva_notice_failed error=%s", type(exc).__name__)

    async def _start(self, adapter, state: State, key: str, target, chat_id, thread_id, **origin) -> None:
        state.progress_message = target
        try:
            cruce = await asyncio.to_thread(self._begin, int(state.user_id), state.contributor_id, **origin)
        except Exception as exc:
            logger.warning("cruce_iva_begin_failed error=%s", type(exc).__name__)
            self.states.pop(key, None)
            staging = origin.get("archivo_staging")
            if staging and STAGING_PATTERN.fullmatch(staging):
                (self.staging_root / staging).unlink(missing_ok=True)
            await self._notify(adapter, state, chat_id, thread_id,
                               "No pude iniciar el cruce. Revisá que tengas acceso a ese cliente y volvé a intentarlo en unos minutos.")
            return
        state.cruce_id = cruce
        state.stage = "running"
        await self._edit(state, PROGRESS_TEXT)
        state.task = asyncio.create_task(self._poll(adapter, state, key, chat_id, thread_id))

    async def _poll(self, adapter, state: State, key: str, chat_id, thread_id) -> None:
        try:
            await self._poll_inner(adapter, state, chat_id, thread_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("cruce_iva_poll_failed error=%s", type(exc).__name__)
            await self._notify(adapter, state, chat_id, thread_id,
                               "Ocurrió un problema al seguir el cruce. Volvé a intentarlo en unos minutos.")
        finally:
            if self.states.get(key) is state:
                self.states.pop(key, None)

    async def _poll_inner(self, adapter, state: State, chat_id, thread_id) -> None:
        deadline = time.monotonic() + self.POLL_TIMEOUT_SECONDS
        failures = 0
        while True:
            try:
                status = await asyncio.to_thread(self._status, int(state.user_id), state.cruce_id)
                failures = 0
            except Exception as exc:
                failures += 1
                logger.warning("cruce_iva_status_failed attempt=%s error=%s", failures, type(exc).__name__)
                if failures >= 3:
                    await self._notify(adapter, state, chat_id, thread_id,
                                       "No pude leer el avance del cruce. Puede seguir en proceso; "
                                       "volvé a pedirlo en unos minutos desde Bancos › Cruce con Libros IVA.")
                    return
                status = None
            if status is not None and status["estado"] in TERMINAL_STATES:
                break
            if time.monotonic() >= deadline:
                await self._notify(adapter, state, chat_id, thread_id,
                                   "El cruce está tardando más de 10 minutos y dejé de esperarlo. "
                                   "Puede seguir en proceso: volvé a pedirlo más tarde desde Bancos › Cruce con Libros IVA.")
                return
            await asyncio.sleep(self.POLL_SECONDS)
        if status["estado"] != "terminado":
            await self._notify(adapter, state, chat_id, thread_id, self.failure_text(status))
            return
        result = status.get("resultado") if isinstance(status.get("resultado"), dict) else {}
        summary = self.summary(result)
        missing = self.missing_books_text(result)
        try:
            output = self._output_path(status.get("salida_relativa"))
        except Exception as exc:
            logger.warning("cruce_iva_output_rejected error=%s", type(exc).__name__)
            await self._notify(adapter, state, chat_id, thread_id,
                               "El cruce terminó, pero no encontré el Excel resultante. Volvé a intentarlo en unos minutos.")
            return
        metadata = {"thread_id": thread_id} if thread_id is not None else None
        delivery = await adapter.send_document(chat_id=str(chat_id), file_path=str(output),
                                               file_name=PdfXlsxFlow._delivery_filename(output),
                                               caption=summary, metadata=metadata)
        if not getattr(delivery, "success", False):
            await self._notify(adapter, state, chat_id, thread_id,
                               "El cruce terminó, pero Telegram no confirmó la entrega del Excel. Volvé a intentarlo.")
            return
        lines = [f"Cruce con Libros IVA terminado: {summary}."]
        if missing:
            lines.append(missing)
        await self._notify(adapter, state, chat_id, thread_id, "\n".join(lines))

    async def _show_planillas(self, query, state: State) -> None:
        try:
            rows = await asyncio.to_thread(self._planillas, int(state.user_id), state.contributor_id)
        except Exception as exc:
            logger.warning("cruce_iva_planillas_failed error=%s", type(exc).__name__)
            await query.edit_message_text("No pude leer los Excel de ese cliente. Volvé a intentarlo en unos minutos.",
                                          reply_markup=self._cancel_keyboard(state.nonce))
            return
        if not rows:
            state.stage = "source"
            await query.edit_message_text(
                f"{TITLE} · {self._client(state)}\n"
                "Todavía no hay Excel de resúmenes bancarios de este cliente. Podés subir uno.",
                reply_markup=self._upload_keyboard(state.nonce))
            return
        state.planillas = rows
        state.selected = set()
        state.stage = "pick"
        await query.edit_message_text(self._pick_text(state), reply_markup=self._pick_keyboard(state))

    async def _offer_callback(self, adapter, query, token: str, chat_id, thread_id, user_id: str) -> bool:
        offer = self.offers.get(token)
        if (offer is None or offer.user_id != str(user_id) or offer.chat_id != str(chat_id)
                or time.monotonic() - offer.created_at > self.OFFER_TTL):
            await query.answer("Esta opción venció. Iniciá el cruce desde Bancos › Cruce con Libros IVA.")
            return True
        key = self._key(chat_id, thread_id, user_id)
        current = self.states.get(key)
        if current and current.stage in ACTIVE_STAGES:
            await query.answer("Ya hay un cruce en curso. Esperá el resultado.")
            return True
        state = State(uuid.uuid4().hex[:10], str(user_id), time.monotonic(), stage="source",
                      contributor_id=offer.contributor_id)
        self.states[key] = state
        await query.answer()
        try:
            rows = await asyncio.to_thread(self._planillas, int(user_id), offer.contributor_id)
        except Exception as exc:
            logger.warning("cruce_iva_offer_planillas_failed error=%s", type(exc).__name__)
            self.states.pop(key, None)
            await query.edit_message_text("No pude preparar el cruce de este Excel. Revisá que tengas acceso a ese cliente "
                                          "o iniciá el cruce desde Bancos › Cruce con Libros IVA.")
            return True
        self.offers.pop(token, None)
        if offer.ruta_relativa in {row.get("ruta") for row in rows}:
            await self._start(adapter, state, key, query.message, chat_id, thread_id,
                              planillas=[offer.ruta_relativa])
            return True
        await query.edit_message_text(
            self._source_text(state, "Ese Excel todavía no está disponible para cruzar.\n"),
            reply_markup=self._source_keyboard(state.nonce))
        return True

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id: str) -> bool:
        if not data.startswith("cx:"):
            return False
        key = self._key(chat_id, thread_id, user_id)
        parts = data.split(":")
        state = self.states.get(key)
        if data == "cx:start":
            if state and state.stage in ACTIVE_STAGES:
                await query.answer("Ya hay un cruce en curso. Esperá el resultado.")
                return True
            state = State(uuid.uuid4().hex[:10], str(user_id), time.monotonic())
            self.states[key] = state
            await query.answer()
            await query.edit_message_text(f"{TITLE}\n{CONTRIBUTOR_PROMPT}", reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if len(parts) == 3 and parts[1] == "of":
            return await self._offer_callback(adapter, query, parts[2], chat_id, thread_id, user_id)
        if (not state or len(parts) < 3 or parts[2] != state.nonce
                or (state.stage not in ACTIVE_STAGES and time.monotonic() - state.created_at > self.TTL)):
            await query.answer("Este pedido venció. Iniciá uno nuevo desde Bancos › Cruce con Libros IVA.")
            return True
        action = parts[1]
        if state.stage in ACTIVE_STAGES:
            await query.answer("El cruce ya está en curso. Esperá el resultado.")
            return True
        state.created_at = time.monotonic()
        if action == "cancel":
            await query.answer()
            self.states.pop(key, None)
            await query.edit_message_text("Cruce con Libros IVA cancelado.")
            return True
        if action == "select" and state.stage == "search" and len(parts) == 4:
            candidate = (state.candidates or {}).get(self._number(parts[3]))
            if not candidate:
                await query.answer("Esta opción ya no está disponible.")
                return True
            state.contributor_id = self._number(candidate["id"])
            state.contributor_name = str(candidate["nombre"])
            state.stage = "source"
            await query.answer()
            await query.edit_message_text(self._source_text(state), reply_markup=self._source_keyboard(state.nonce))
            return True
        if action == "pick" and state.stage == "source":
            await query.answer()
            await self._show_planillas(query, state)
            return True
        if action == "up" and state.stage == "source":
            state.stage = "upload"
            await query.answer()
            await query.edit_message_text(
                f"{TITLE} · {self._client(state)}\n"
                "Mandame el Excel (.xlsx) como documento. Puede pesar hasta 15 MB.",
                reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if action == "t" and state.stage == "pick" and len(parts) == 4:
            index = int(parts[3]) if parts[3].isdigit() else -1
            if not 0 <= index < len(state.planillas):
                await query.answer("Esta opción ya no está disponible.")
                return True
            state.selected ^= {index}
            await query.answer()
            await query.edit_message_text(self._pick_text(state), reply_markup=self._pick_keyboard(state))
            return True
        if action == "go" and state.stage == "pick":
            if not state.selected:
                await query.answer("Elegí al menos un Excel.")
                return True
            await query.answer()
            paths = [state.planillas[index]["ruta"] for index in sorted(state.selected)]
            await self._start(adapter, state, key, query.message, chat_id, thread_id, planillas=paths)
            return True
        await query.answer("Esta opción ya no está disponible.")
        return True

    async def text(self, adapter, message) -> bool:
        user = getattr(message, "from_user", None)
        key = self._key(message.chat_id, getattr(message, "message_thread_id", None), getattr(user, "id", ""))
        state = self.states.get(key)
        if not state or state.stage not in {"search", "upload"}:
            return False
        if time.monotonic() - state.created_at > self.TTL:
            self.states.pop(key, None)
            return False
        if state.stage == "upload":
            await message.reply_text("Esperaba el Excel (.xlsx) como documento. Mandalo adjunto o tocá Cancelar.",
                                     reply_markup=self._cancel_keyboard(state.nonce))
            return True
        try:
            rows = await asyncio.to_thread(self._search, int(state.user_id), message.text or "")
            offer = ContributorOffer.from_rows(rows)
        except Exception:
            await message.reply_text("No pude buscar el cliente. Volvé a intentarlo en unos minutos.",
                                     reply_markup=self._cancel_keyboard(state.nonce))
            return True
        state.created_at = time.monotonic()
        if offer.status == "empty":
            await message.reply_text("No encontré un cliente autorizado con ese nombre, CUIT o alias.",
                                     reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if offer.status == "single":
            candidate = offer.single
            state.contributor_id = self._number(candidate["id"])
            state.contributor_name = str(candidate["nombre"])
            state.stage = "source"
            await message.reply_text(self._source_text(state), reply_markup=self._source_keyboard(state.nonce))
            return True
        state.candidates = offer.candidates
        await message.reply_text(
            MULTIPLE_CONTRIBUTORS_TEXT,
            reply_markup=offer.keyboard(callback_prefix="cx", nonce=state.nonce,
                                        cancel_text=menu_label("✕", "Cancelar")))
        return True

    async def document(self, adapter, message) -> bool:
        user = getattr(message, "from_user", None)
        chat_id = message.chat_id
        thread_id = getattr(message, "message_thread_id", None)
        key = self._key(chat_id, thread_id, getattr(user, "id", ""))
        state = self.states.get(key)
        if not state or state.stage != "upload":
            return False
        if time.monotonic() - state.created_at > self.TTL:
            self.states.pop(key, None)
            return False
        document = getattr(message, "document", None)
        raw_name = str(getattr(document, "file_name", "") or "")
        size = int(getattr(document, "file_size", 0) or 0)
        if not document or not raw_name.lower().endswith(".xlsx"):
            await message.reply_text("Esperaba un Excel con extensión .xlsx. Mandalo de nuevo como documento.",
                                     reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            await message.reply_text("El Excel supera el límite de 15 MB o Telegram no informó su tamaño.",
                                     reply_markup=self._cancel_keyboard(state.nonce))
            return True
        state.stage = "running"
        progress = await message.reply_text("Recibiendo el Excel…")
        state.progress_message = progress
        try:
            telegram_file = await document.get_file()
            data = bytes(await telegram_file.download_as_bytearray())
            if not data or len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("cruce_iva_tamano_invalido")
            staging = await asyncio.to_thread(self._write_staging, int(state.user_id), data)
        except Exception as exc:
            logger.warning("cruce_iva_upload_failed error=%s", type(exc).__name__)
            self.states.pop(key, None)
            await self._notify(adapter, state, chat_id, thread_id,
                               "No pude recibir el Excel. Volvé a intentarlo desde Bancos › Cruce con Libros IVA.")
            return True
        await self._start(adapter, state, key, progress, chat_id, thread_id,
                          archivo_nombre=self._upload_name(raw_name), archivo_staging=staging)
        return True
