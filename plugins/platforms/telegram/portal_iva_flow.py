"""Flujo Telegram para descargar CSV de Ventas y Compras desde Portal IVA."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from hermes_constants import get_hermes_home

from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.bot_run_history import BotRunHistory, RunHandle
from plugins.platforms.telegram.contributor_selector import (
    CONTRIBUTOR_PROMPT,
    MULTIPLE_CONTRIBUTORS_TEXT,
    ContributorOffer,
)
from plugins.platforms.telegram.fiscal_scope import (
    ACCOUNT_NOT_LINKED_MESSAGE,
    AccountNotLinked,
    FiscalScope,
    InvalidCuit,
)
from plugins.platforms.telegram.fiscal_credentials import (
    lookup_connection,
    sanitized_psql_failure,
    verification_connection,
    verify_representation,
)

try:
    import fcntl
except ImportError:  # Windows gateway: keep Telegram importable, hide this Linux-only feature.
    fcntl = None

logger = logging.getLogger(__name__)
_CLIENTES_ROOT = Path(os.environ.get("CONTABOT_CLIENTES_ROOT", Path.home() / "clientes"))
_EXECUTOR = Path(os.environ.get(
    "CONTABOT_PORTAL_IVA_EXECUTOR", Path.home() / "procedimientos/portal-iva/portal_iva.py"
))
_BATCH_EXECUTOR_RAW = os.environ.get("CONTABOT_PORTAL_IVA_BATCH_EXECUTOR")
_BATCH_EXECUTOR = Path(_BATCH_EXECUTOR_RAW) if _BATCH_EXECUTOR_RAW else None
_UV = get_hermes_home() / "bin/uv"
_PERIOD = re.compile(r"(0[1-9]|1[0-2])/[0-9]{4}")
_STATE_ROOT = Path(os.environ.get(
    "CONTABOT_STATE_ROOT", Path.home() / ".local/state/contabot"
))
_LOCK_ROOT = _STATE_ROOT / "portal-iva/telegram-locks"
_CAPTCHA_ROOT = _STATE_ROOT / "portal-iva/runs"
_BATCH_RESULT_ROOT = _STATE_ROOT / "portal-iva/telegram-results"
_RUN_TIMEOUT_SECONDS = 1800
_PIPE_STREAM_LIMIT = 256 * 1024
_PIPE_CAPTURE_LIMIT = 4 * 1024 * 1024
_TELEGRAM_ZIP_LIMIT = 45_000_000
_TELEGRAM_FILE_LIMIT = 50_000_000
_ZIP_PACK_BUDGET = 44_000_000
_CAPTCHA_TIMEOUT_SECONDS = 300
_CAPTCHA_SOLUTION = re.compile(r"[A-Za-z0-9]{4,20}")
_UNVERIFIED_KEY_HELP = (
    "Si un cliente no aparece en la lista, su clave de ARCA no está cargada "
    "o todavía no fue verificada."
)


@dataclass
class FlowState:
    user_id: str
    nonce: str
    stage: str
    operation: str = "generar"
    contributor_id: int | None = None
    slug: str | None = None
    cuit: str | None = None
    nombre: str | None = None
    period: str | None = None
    progress_message: Any = None
    progress_label: str = "Generando CSV de período nuevo…"
    cancelled: bool = False
    captcha_nonce: str | None = None
    captcha_response: asyncio.Future | None = None
    created_at: float = field(default_factory=time.monotonic)
    candidates: dict[int, dict[str, Any]] = field(default_factory=dict)
    scope_item: dict[str, Any] | None = None
    selected_clients: list[dict[str, Any]] = field(default_factory=list)
    period_from: str | None = None
    period_to: str | None = None
    batch_months: int = 0
    batch_files: dict[int, dict[tuple[str, str], str]] = field(default_factory=dict)


class PortalIvaFlow:
    """Privileged Telegram flow; credentials are read only by portal_iva.py."""

    def __init__(self, *, executor: Path = _EXECUTOR, batch_executor: Path | None = _BATCH_EXECUTOR,
                 uv: Path = _UV,
                 clients_root: Path = _CLIENTES_ROOT, captcha_root: Path = _CAPTCHA_ROOT,
                 batch_result_root: Path = _BATCH_RESULT_ROOT,
                 query_connection=None, write_connection=None, runtime_python: Path | None = None,
                 history: BotRunHistory | None = None) -> None:
        # CCMA/SCT reuse only identity lookup, with their restricted connection.
        # Defaults preserve the existing Portal IVA executor and DB connection.
        self.query_connection = query_connection or lookup_connection
        self.write_connection = write_connection or verification_connection
        self.runtime_python = runtime_python
        self.executor = Path(executor)
        self.batch_executor = Path(batch_executor) if batch_executor is not None else self.executor.with_name("portal_iva_lote.py")
        self.uv = Path(uv)
        self.clients_root = Path(clients_root)
        self.captcha_root = Path(captcha_root)
        self.batch_result_root = Path(batch_result_root)
        self.states: dict[str, FlowState] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.execution_locks: dict[str, str] = {}
        self.execution_lock_fds: dict[str, int] = {}
        self.history = history

    def available(self) -> bool:
        return fcntl is not None and self.executor.is_file() and self.uv.is_file() and self.clients_root.is_dir()

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _visible_cuit(cuit: str) -> str:
        return f"{cuit[:2]}-******-{cuit[-1:]}" if re.fullmatch(r"\d{11}", cuit or "") else "CUIT oculto"

    @staticmethod
    def _progress_text(stage: str) -> str | None:
        return {
            "generar": "Generando CSV de período nuevo…",
            "buscar_presentado": "Buscando presentación…",
            "descargar_ventas": "Descargando Libro IVA Ventas…",
            "descargar_compras": "Descargando Libro IVA Compras…",
            "validar_archivos": "Validando archivos…",
        }.get(stage)

    @staticmethod
    def _sql_scalar(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    @staticmethod
    async def _send(adapter, chat_id: Any, text: str, thread_id: Any = None, reply_markup: Any = None):
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await adapter._bot.send_message(**kwargs)

    async def _reject(self, *, user_id: Any, operation: str, key_material: str,
                      code: str, text: str, contributor_id: int | None = None) -> None:
        if self.history is None:
            return
        operation_code = (
            "portal_iva_generar_csv" if operation == "generar"
            else "portal_iva_lote_presentados" if operation == "descargar-lote"
            else "portal_iva_descargar_presentados"
        )
        try:
            await self.history.reject(
                telegram_id=int(user_id), operation=operation_code,
                key_material=key_material, reference="Portal IVA",
                reason_code=code, reason_text=text, contributor_id=contributor_id,
            )
        except Exception:
            logger.exception("[PORTAL-IVA] terminal rejection could not be persisted")

    @staticmethod
    def _run_query(sql: str, connection) -> list[dict[str, Any]]:
        command, environment = connection()
        run = subprocess.run(
            [*command, "-At", "-v", "VERBOSITY=sqlstate", "-c", sql], env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        if run.returncode:
            sqlstate, first_line = sanitized_psql_failure(run.stderr)
            logger.error(
                "[PORTAL-IVA] database query failed returncode=%s sqlstate=%s first_line=%s",
                run.returncode, sqlstate, first_line,
            )
            code = (
                "PORTAL_IVA_DATABASE_PERMISSIONS"
                if sqlstate == "42501"
                else "PORTAL_IVA_DATABASE_UNAVAILABLE"
            )
            raise RuntimeError(code)
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def _query(self, sql: str) -> list[dict[str, Any]]:
        return self._run_query(sql, self.query_connection)

    def _write_query(self, sql: str) -> list[dict[str, Any]]:
        return self._run_query(sql, self.write_connection)

    def _scope(self) -> FiscalScope:
        return FiscalScope(self._query, "ARCA")

    def _search(self, term: str, telegram_id: Any) -> list[dict[str, Any]]:
        return self._scope().search(telegram_id, term)

    def _by_id(self, item_id: int, telegram_id: Any) -> list[dict[str, Any]]:
        return self._scope().by_id(telegram_id, item_id)

    def _register_terminal_batch(self, run_id: int, clients: list[dict[str, Any]],
                                 periods: list[str], state: str, reason: str) -> None:
        if state not in {"fallido", "cancelado"} or not clients or not periods:
            raise RuntimeError("PORTAL_IVA_TERMINAL_BATCH_INVALID")
        payload = {"casos": [
            {"id_contribuyente": int(client["id"]), "periodo": period, "ok": False,
             "estado": state, "motivo": reason}
            for client in clients for period in periods
        ]}
        encoded = self._sql_scalar(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        rows = self._write_query(
            "SELECT console.fn_portal_iva_lote_registrar_telegram("
            f"{int(run_id)},'{periods[0]}-01'::date,'{periods[-1]}-01'::date,"
            f"convert_from(decode('{encoded}','base64'),'UTF8')::jsonb)::text;"
        )
        if len(rows) != 1 or not isinstance(rows[0], int):
            raise RuntimeError("PORTAL_IVA_TERMINAL_BATCH_NOT_RECORDED")

    @staticmethod
    def _cancel_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"pi:cancel:{nonce}")]])

    @staticmethod
    def _candidate_keyboard(candidates: list[dict[str, Any]], nonce: str) -> InlineKeyboardMarkup:
        return ContributorOffer.from_rows(candidates).keyboard(
            callback_prefix="pi",
            nonce=nonce,
            cancel_text=menu_label("❌", "Cancelar"),
            button_factory=InlineKeyboardButton,
            markup_factory=InlineKeyboardMarkup,
        )

    async def start(self, adapter, query, chat_id: Any, thread_id: Any, user_id: str, operation: str) -> None:
        if operation not in {"generar", "descargar-presentados", "descargar-lote"}:
            await query.answer("Operación Portal IVA inválida.")
            return
        key = self._key(chat_id, thread_id, user_id)
        if key in self.tasks:
            await query.answer("Ya hay una descarga Portal IVA en curso.")
            await self._reject(user_id=user_id, operation=operation, key_material=f"active:{key}:{getattr(getattr(query, 'message', None), 'message_id', 0)}", code="consulta_en_curso", text="Ya había una operación Portal IVA en ejecución para este usuario.")
            return
        try:
            await asyncio.to_thread(self._scope().require_actor, user_id)
            await asyncio.to_thread(self._scope().require_permission, user_id, "operaciones.ejecutar")
        except AccountNotLinked:
            await query.answer("Cuenta no vinculada")
            await self._send(adapter, chat_id, ACCOUNT_NOT_LINKED_MESSAGE, thread_id)
            return
        except PermissionError:
            await query.answer("No tenés permiso para ejecutar operaciones fiscales.")
            return
        except Exception as exc:
            code = self._safe_error_code(exc)
            logger.error("[PORTAL-IVA] start failed code=%s", code)
            try:
                await query.answer("Portal IVA no pudo iniciarse.")
            except Exception:
                logger.info("[PORTAL-IVA] callback answer failed after start error")
            text = (
                "Portal IVA no pudo iniciarse porque no se pudo acceder a la "
                "configuración fiscal local. No se consultó ARCA; requiere mantenimiento local."
            )
            try:
                await self._send(adapter, chat_id, text, thread_id)
            except Exception:
                logger.info("[PORTAL-IVA] user notification failed after start error")
            await self._reject(
                user_id=user_id, operation=operation,
                key_material=(
                    f"start:{getattr(getattr(query, 'message', None), 'message_id', 0)}:{operation}"
                ),
                code="base_fiscal_no_disponible", text=text,
            )
            return
        if operation == "descargar-lote" and not self.batch_executor.is_file():
            await query.answer("El lote de Portal IVA requiere mantenimiento local.")
            text = (
                "El lote de Libros IVA no pudo iniciarse porque falta su ejecutor local. "
                "No se consultó ARCA y no se creó ningún borrador."
            )
            await self._send(adapter, chat_id, text, thread_id)
            await self._reject(
                user_id=user_id, operation=operation,
                key_material=(
                    f"runtime:{getattr(getattr(query, 'message', None), 'message_id', 0)}:{operation}"
                ),
                code="ejecutor_lote_no_disponible", text=text,
            )
            return
        self.states[key] = FlowState(
            user_id=user_id, nonce=uuid.uuid4().hex[:10], stage="client", operation=operation,
            progress_label=("Generando CSV de período nuevo…" if operation == "generar" else "Buscando presentación…"),
            created_at=time.monotonic(),
        )
        await query.answer("Portal IVA")
        prompt = CONTRIBUTOR_PROMPT
        if operation == "descargar-lote":
            prompt += "\n\n" + _UNVERIFIED_KEY_HELP
        await self._send(
            adapter, chat_id, prompt, thread_id,
            self._cancel_keyboard(self.states[key].nonce),
        )

    async def callback(self, adapter, query, data: str, chat_id: Any, thread_id: Any, user_id: str) -> bool:
        key = self._key(chat_id, thread_id, user_id)
        operation = {"pi:generar": "generar", "pi:descargar": "descargar-presentados", "pi:lote": "descargar-lote"}.get(data)
        if operation:
            await self.start(adapter, query, chat_id, thread_id, user_id, operation)
            return True
        if data == "pi:start":
            await query.answer("Elegí una operación desde Portal IVA.")
            return True
        cancel = re.fullmatch(r"pi:cancel:([0-9a-f]{10})", data)
        if cancel:
            state = self.states.get(key)
            if not state or state.nonce != cancel.group(1):
                await query.answer("Esta ejecución ya no está activa.")
                return True
            if state.stage == "delivering":
                await query.answer("Los CSV ya están en entrega; no se pueden cancelar.")
                return True
            await query.answer("Cancelando Portal IVA…")
            state.cancelled = True
            if state.captcha_response is not None and not state.captcha_response.done():
                state.captcha_response.set_result(None)
            proc = self.processes.get(key)
            if proc is not None and proc.returncode is None:
                await self._terminate_process(proc)
            text = "Portal IVA cancelado. Si el procedimiento ya inició una escritura externa, el borrador puede haber quedado modificado; la evidencia quedó preservada."
            if state.progress_message is None:
                self.states.pop(key, None)
                await query.edit_message_text(text, reply_markup=None)
            else:
                await self._edit_progress(state, text)
            return True
        select = re.fullmatch(r"pi:select:([0-9a-f]{10}):(\d+)", data)
        if not select:
            return False
        state = self.states.get(key)
        if state and state.stage != "running" and time.monotonic() - state.created_at > 600:
            self.states.pop(key, None)
            await self._reject(user_id=user_id, operation=state.operation, key_material=f"expired:{state.nonce}", code="seleccion_vencida", text="La selección del contribuyente venció antes de iniciar la operación.", contributor_id=state.contributor_id)
            await query.answer("Esta solicitud venció. Iniciá Portal IVA nuevamente.")
            return True
        if not state or state.nonce != select.group(1) or state.stage not in {"client", "batch_clients"}:
            await query.answer("Esta selección venció. Iniciá Portal IVA nuevamente.")
            return True
        try:
            selected_id = int(select.group(2))
            if selected_id not in state.candidates:
                await query.answer("La opción ya no está disponible.")
                await self._reject(user_id=user_id, operation=state.operation, key_material=f"stale:{state.nonce}:{selected_id}", code="contribuyente_no_disponible", text="El contribuyente elegido ya no estaba autorizado.")
                return True
            found = await asyncio.to_thread(self._by_id, selected_id, state.user_id)
            if state.operation == "descargar-lote":
                found = [item for item in found if item.get("access_id")]
        except Exception:
            await query.answer("No pude validar la opción en la base. Probá nuevamente.")
            await self._reject(user_id=user_id, operation=state.operation, key_material=f"database:{state.nonce}:{select.group(2)}", code="base_no_disponible", text="La base canónica no permitió validar el contribuyente antes de ejecutar.")
            return True
        if not found:
            await query.answer("La opción ya no está disponible.")
            await self._reject(user_id=user_id, operation=state.operation, key_material=f"revalidate:{state.nonce}:{selected_id}", code="contribuyente_no_autorizado", text="El contribuyente elegido ya no tenía acceso o representación válidos.")
            return True
        await query.answer("Contribuyente seleccionado")
        await self._select(adapter, chat_id, thread_id, state, found[0])
        return True

    async def text(self, adapter, message) -> bool:
        chat_id, user_id = message.chat_id, str(message.from_user.id)
        thread_id = getattr(message, "message_thread_id", None)
        key = self._key(chat_id, thread_id, user_id)
        state = self.states.get(key)
        if not state:
            return False
        if state.stage not in {"running", "captcha", "delivering"} and time.monotonic() - state.created_at > 600:
            self.states.pop(key, None)
            await self._reject(user_id=user_id, operation=state.operation, key_material=f"expired:{state.nonce}", code="solicitud_vencida", text="La solicitud venció antes de iniciar la operación.", contributor_id=state.contributor_id)
            await self._send(adapter, chat_id, "La solicitud venció. Iniciá Portal IVA nuevamente.", thread_id)
            return True
        if state.stage == "captcha":
            solution = (message.text or "").strip()
            if not _CAPTCHA_SOLUTION.fullmatch(solution):
                await self._send(
                    adapter, chat_id,
                    "La solución debe tener entre 4 y 20 letras o números. Mirá la imagen y respondé sólo con esos caracteres.",
                    thread_id,
                )
                return True
            pending = state.captcha_response
            if pending is None or pending.done():
                await self._send(adapter, chat_id, "Ese captcha ya no está activo.", thread_id)
                return True
            state.progress_label = "Validando captcha…"
            await self._edit_progress(state, state.progress_label, keyboard=self._cancel_keyboard(state.nonce))
            pending.set_result(solution)
            return True
        if state.stage in {"running", "delivering"}:
            await self._send(adapter, chat_id, "Portal IVA ya está en ejecución para esta solicitud.", thread_id)
            return True
        if state.stage in {"batch_from", "batch_to"}:
            visible_period = (message.text or "").strip()
            if not _PERIOD.fullmatch(visible_period):
                await self._send(adapter, chat_id, "Ingresá el período como MM/AAAA. Ejemplo: 08/2026.", thread_id)
                return True
            period = f"{visible_period[3:]}-{visible_period[:2]}"
            if state.stage == "batch_from":
                state.period_from = period
                state.stage = "batch_to"
                await self._send(adapter, chat_id, "Ingresá el último período como MM/AAAA.", thread_id)
                return True
            state.period_to = period
            if state.period_from is None or state.period_from > state.period_to:
                state.stage = "batch_from"
                await self._send(adapter, chat_id, "El rango no es válido. Ingresá nuevamente el primer período como MM/AAAA.", thread_id)
                return True
            state.stage = "running"
            state.progress_label = "Preparando lote de Libros IVA…"
            state.progress_message = await self._send(adapter, chat_id, state.progress_label, thread_id, self._cancel_keyboard(state.nonce))
            task = asyncio.create_task(self._run_batch(adapter, chat_id, thread_id, key, state))
            self.tasks[key] = task
            task.add_done_callback(lambda done: self._release(key, done))
            return True
        if state.stage == "period":
            visible_period = (message.text or "").strip()
            if not _PERIOD.fullmatch(visible_period):
                await self._send(adapter, chat_id, "Ingresá el período como MM/AAAA. Ejemplo: 08/2026.", thread_id)
                return True
            period = f"{visible_period[3:]}-{visible_period[:2]}"
            if key in self.tasks:
                await self._send(adapter, chat_id, "Ya hay una descarga Portal IVA en curso.", thread_id)
                return True
            state.period = period
            state.stage = "running"
            state.progress_label = (
                "Generando CSV de período nuevo…"
                if state.operation == "generar" else "Buscando presentación…"
            )
            state.progress_message = await self._send(
                adapter, chat_id, state.progress_label, thread_id, self._cancel_keyboard(state.nonce),
            )
            task = asyncio.create_task(self._run(adapter, chat_id, thread_id, key, state))
            self.tasks[key] = task
            task.add_done_callback(lambda done: self._release(key, done))
            return True
        if state.operation == "descargar-lote" and state.stage == "batch_clients" and (message.text or "").strip().casefold() == "listo":
            if not state.selected_clients:
                await self._send(adapter, chat_id, "Elegí al menos un contribuyente.", thread_id)
                return True
            state.stage = "batch_from"
            await self._send(adapter, chat_id, "Ingresá el primer período como MM/AAAA.", thread_id)
            return True
        try:
            candidates = await asyncio.to_thread(self._search, message.text or "", state.user_id)
            if state.operation == "descargar-lote":
                candidates = [item for item in candidates if item.get("access_id")]
        except InvalidCuit:
            await self._send(adapter, chat_id, "El CUIT ingresado no es válido.", thread_id)
            return True
        except Exception:
            await self._send(adapter, chat_id, "No pude consultar la base canónica. Probá nuevamente.", thread_id)
            return True
        offer = ContributorOffer.from_rows(candidates)
        state.candidates = offer.candidates
        if offer.status == "empty":
            empty_text = "No hay un contribuyente ARCA activo con acceso y representación válidos que coincida. Probá con nombre, CUIT o slug."
            if state.operation == "descargar-lote":
                empty_text += "\n\n" + _UNVERIFIED_KEY_HELP
            await self._send(adapter, chat_id, empty_text, thread_id)
            if len(re.sub(r"\D", "", message.text or "")) == 11:
                await self._reject(user_id=user_id, operation=state.operation, key_material=f"unauthorized:{state.nonce}", code="contribuyente_no_autorizado", text="El CUIT solicitado no tenía acceso y representación ARCA autorizados.")
            return True
        if offer.status == "single":
            await self._select(adapter, chat_id, thread_id, state, offer.single)
            return True
        await self._send(
            adapter,
            chat_id,
            MULTIPLE_CONTRIBUTORS_TEXT,
            thread_id,
            self._candidate_keyboard(candidates, state.nonce),
        )
        return True

    async def _select(self, adapter, chat_id: Any, thread_id: Any, state: FlowState, item: dict[str, Any]) -> None:
        if state.operation == "descargar-lote":
            if state.selected_clients and int(state.selected_clients[0]["study_id"]) != int(item["study_id"]):
                await self._send(
                    adapter, chat_id,
                    "Un lote sólo puede contener contribuyentes del mismo estudio. "
                    "Terminá este lote o cancelalo para iniciar otro.",
                    thread_id,
                )
                return
            if not any(int(current["id"]) == int(item["id"]) for current in state.selected_clients):
                state.selected_clients.append(dict(item))
            state.stage = "batch_clients"
            await self._send(adapter, chat_id, f"Agregado: {item['nombre']}. Escribí otro nombre/slug o LISTO para continuar.", thread_id)
            return
        state.contributor_id = int(item["id"])
        state.slug = str(item["slug"])
        state.cuit = str(item["cuit"])
        state.nombre = str(item["nombre"])
        state.scope_item = dict(item)
        state.stage = "period"
        await self._send(adapter, chat_id, "Ingresá el período como MM/AAAA. Ejemplo: 08/2026.", thread_id)

    def _command(self, slug: str, period: str, operation: str) -> list[str]:
        if not self.available():
            raise RuntimeError("PORTAL_IVA_RUNTIME_UNAVAILABLE")
        if operation not in {"generar", "descargar-presentados"}:
            raise RuntimeError("PORTAL_IVA_OPERATION_INVALID")
        return [
            str(self.uv), "run", "--with", "selenium", "xvfb-run", "-a",
            "python3", str(self.executor), "--cliente", slug, "--periodo", period,
            "--operacion", operation, "--captcha-stdin",
        ]

    def _batch_command(self, state: FlowState, history_run_id: int | None,
                       result_file: Path) -> list[str]:
        if not self.batch_executor.is_file() or not state.period_from or not state.period_to:
            raise RuntimeError("PORTAL_IVA_BATCH_RUNTIME_UNAVAILABLE")
        command = [str(self.uv), "run", "--with", "selenium", "--with", "openpyxl", "xvfb-run", "-a",
                   "python3", str(self.batch_executor)]
        periods = self._period_range(state.period_from, state.period_to)
        for client in state.selected_clients:
            if not client.get("access_id") or not client.get("representative_id"):
                raise RuntimeError("PORTAL_IVA_BATCH_BINDING_UNAVAILABLE")
            for period in periods:
                command += ["--caso", (
                    f"{client['slug']}:{period}:{int(client['access_id'])}:"
                    f"{int(client['representative_id'])}"
                )]
        command += ["--captcha-stdin", "--result-file", str(result_file)]
        if history_run_id is not None:
            command += ["--history-run-id", str(history_run_id)]
        return command

    def _new_batch_result_target(self) -> tuple[Path, Path]:
        root = self.batch_result_root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if (root.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise RuntimeError("PORTAL_IVA_RESULT_ROOT_INVALID")
        directory = Path(tempfile.mkdtemp(prefix="run-", dir=root))
        directory.chmod(0o700)
        return directory, directory / "result.json"

    @staticmethod
    def _read_batch_result(path: Path) -> dict[str, Any]:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 4 * 1024 * 1024):
                raise RuntimeError("PORTAL_IVA_RESULT_FILE_INVALID")
            chunks: list[bytes] = []
            remaining = info.st_size + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        return PortalIvaFlow._parse_result(raw)

    def _acquire_execution_lock(self, key: str) -> None:
        if fcntl is None:
            raise RuntimeError("PORTAL_IVA_RUNTIME_UNAVAILABLE")
        _LOCK_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(_LOCK_ROOT, 0o700)
        path = _LOCK_ROOT / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("PORTAL_IVA_EXECUTION_ALREADY_RUNNING") from exc
        self.execution_lock_fds[key] = fd

    def _release_execution_lock(self, key: str) -> None:
        fd = self.execution_lock_fds.pop(key, None)
        if fd is not None and fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await proc.wait()

    async def _edit_progress(self, state: FlowState, text: str, *, keyboard: Any = None) -> None:
        message = state.progress_message
        if message is None:
            return
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except Exception:
            logger.warning("[PORTAL-IVA] progress edit failed")

    async def _ticker(self, state: FlowState, started: float) -> None:
        while not state.cancelled:
            await asyncio.sleep(10)
            if state.cancelled:
                return
            elapsed = int(time.monotonic() - started)
            await self._edit_progress(state, f"{state.progress_label} {elapsed} s", keyboard=self._cancel_keyboard(state.nonce))
    async def _batch_progress_event(self, state: FlowState, kind: str, payload: dict[str, Any]) -> None:
        try:
            contributor_id = int(payload["id_contribuyente"])
            period = str(payload["periodo"])
        except (KeyError, TypeError, ValueError):
            return
        client = next((item for item in state.selected_clients if int(item["id"]) == contributor_id), None)
        if client is None or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
            return
        if state.period_from and period < state.period_from:
            return
        if state.period_to and period > state.period_to:
            return
        files = state.batch_files.setdefault(contributor_id, {})
        if kind == "file":
            book, status = payload.get("tipo"), payload.get("estado")
            if book not in {"ventas", "compras"} or status not in {"descargado", "sin_libro", "error"}:
                return
            key = (period, book)
            if key in files:
                return
            files[key] = status
        elif kind != "case_start":
            return
        downloaded = sum(value == "descargado" for value in files.values())
        total = state.batch_months * 2 - sum(value == "sin_libro" for value in files.values())
        no_books = sorted({period for (period, _), status in files.items() if status == "sin_libro"})
        errors = sorted((period, book) for (period, book), status in files.items() if status == "error")
        notes = [f"{item[5:]}/{item[:4]} sin libro presentado" for item in no_books]
        notes.extend(f"{book.capitalize()} {item[5:]}/{item[:4]}: error" for item, book in errors)
        detail = f" ({'; '.join(notes)})" if notes else ""
        state.progress_label = (
            f"▎ Se descargaron {downloaded}/{total} archivos de {client['slug']}{detail}"
        )
        await self._edit_progress(state, state.progress_label, keyboard=self._cancel_keyboard(state.nonce))

    @staticmethod
    def _parse_result(raw: bytes) -> dict[str, Any]:
        try:
            lines = [line for line in raw.decode("utf-8", errors="strict").splitlines() if line.strip()]
        except UnicodeDecodeError as exc:
            raise RuntimeError("PORTAL_IVA_STDOUT_INVALID") from exc
        if len(lines) != 1:
            raise RuntimeError("PORTAL_IVA_STDOUT_INVALID")
        try:
            result = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise RuntimeError("PORTAL_IVA_STDOUT_INVALID") from exc
        if not isinstance(result, dict):
            raise RuntimeError("PORTAL_IVA_STDOUT_INVALID")
        return result

    def _validated_captcha_path(self, raw_path: str, nonce: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", nonce):
            raise RuntimeError("PORTAL_IVA_CAPTCHA_REQUEST_INVALID")
        candidate = Path(raw_path)
        if not candidate.is_absolute() or candidate.name != f"captcha-{nonce}.png":
            raise RuntimeError("PORTAL_IVA_CAPTCHA_REQUEST_INVALID")
        try:
            root = self.captcha_root.resolve(strict=True)
            relative = candidate.relative_to(root)
            cursor = root
            for part in relative.parts[:-1]:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise RuntimeError("PORTAL_IVA_CAPTCHA_PATH_INVALID")
            if candidate.is_symlink():
                raise RuntimeError("PORTAL_IVA_CAPTCHA_PATH_INVALID")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise RuntimeError("PORTAL_IVA_CAPTCHA_PATH_INVALID")
            metadata = resolved.stat()
        except (OSError, ValueError) as exc:
            raise RuntimeError("PORTAL_IVA_CAPTCHA_PATH_INVALID") from exc
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077 or not 0 < metadata.st_size <= 1_000_000):
            raise RuntimeError("PORTAL_IVA_CAPTCHA_PATH_INVALID")
        return resolved

    async def _answer_captcha(self, adapter, proc, state: FlowState, chat_id: Any,
                              thread_id: Any, payload: bytes) -> None:
        if state.captcha_response is not None:
            raise RuntimeError("PORTAL_IVA_CAPTCHA_REQUEST_DUPLICATE")
        try:
            request = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("PORTAL_IVA_CAPTCHA_REQUEST_INVALID") from exc
        if not isinstance(request, dict) or set(request) != {"nonce", "path"}:
            raise RuntimeError("PORTAL_IVA_CAPTCHA_REQUEST_INVALID")
        nonce, raw_path = request.get("nonce"), request.get("path")
        if not isinstance(nonce, str) or not isinstance(raw_path, str):
            raise RuntimeError("PORTAL_IVA_CAPTCHA_REQUEST_INVALID")
        captcha_path = self._validated_captcha_path(raw_path, nonce)
        if proc.stdin is None:
            raise RuntimeError("PORTAL_IVA_CAPTCHA_STDIN_UNAVAILABLE")
        state.stage = "captcha"
        state.captcha_nonce = nonce
        state.captcha_response = asyncio.get_running_loop().create_future()
        state.progress_label = "Esperando la solución del captcha…"
        try:
            descriptor = os.open(captcha_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as image:
                kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "photo": image,
                    "caption": "ARCA solicita un captcha. Respondé sólo con los caracteres de la imagen.",
                    "reply_markup": self._cancel_keyboard(state.nonce),
                }
                if thread_id is not None:
                    kwargs["message_thread_id"] = thread_id
                await adapter._bot.send_photo(**kwargs)
            solution = await asyncio.wait_for(state.captcha_response, timeout=_CAPTCHA_TIMEOUT_SECONDS)
            if state.cancelled or solution is None:
                raise RuntimeError("PORTAL_IVA_CANCELLED")
            answer = json.dumps({"nonce": nonce, "solution": solution}, separators=(",", ":")) + "\n"
            proc.stdin.write(answer.encode("utf-8"))
            await proc.stdin.drain()
            state.stage = "running"
            state.progress_label = "Ingresando a ARCA…"
            await self._edit_progress(state, state.progress_label, keyboard=self._cancel_keyboard(state.nonce))
        except TimeoutError as exc:
            raise RuntimeError("PORTAL_IVA_CAPTCHA_TIMEOUT") from exc
        finally:
            state.captcha_nonce = None
            state.captcha_response = None

    async def _communicate_with_progress(self, proc, state: FlowState, *, adapter=None,
                                         chat_id: Any = None, thread_id: Any = None,
                                         batch_event=None) -> tuple[bytes, bytes]:
        """Separate structured progress from the final JSON on either pipe.

        xvfb-run merges the wrapped command's stderr into stdout, so the
        executor's progress channel can arrive through either stream.
        """
        if not getattr(proc, "stdout", None) or not getattr(proc, "stderr", None):
            return await proc.communicate()

        async def consume(stream) -> bytes:
            captured = bytearray()
            pending = bytearray()
            discarding = False

            async def handle_line(line: bytes) -> None:
                captcha_marker = b"PORTAL_IVA_CAPTCHA:"
                if line.startswith(captcha_marker):
                    if adapter is None:
                        raise RuntimeError("PORTAL_IVA_CAPTCHA_CHANNEL_UNAVAILABLE")
                    await self._answer_captcha(
                        adapter, proc, state, chat_id, thread_id,
                        line[len(captcha_marker):].strip(),
                    )
                    return
                marker = b"PORTAL_IVA_PROGRESS:"
                if line.startswith(marker):
                    stage = line[len(marker):].decode("ascii", errors="ignore").strip()
                    text = self._progress_text(stage)
                    if text and not (batch_event is not None and any(state.batch_files.values())):
                        state.progress_label = text
                        await self._edit_progress(state, text, keyboard=self._cancel_keyboard(state.nonce))
                    return
                if batch_event is not None:
                    for prefix, kind in ((b"PORTAL_IVA_CASE_START:", "case_start"),
                                         (b"PORTAL_IVA_FILE:", "file")):
                        if line.startswith(prefix):
                            if len(line) <= 65536:
                                try:
                                    payload = json.loads(line[len(prefix):])
                                except (UnicodeDecodeError, json.JSONDecodeError):
                                    return
                                if isinstance(payload, dict):
                                    await batch_event(kind, payload)
                            return
                    if line.startswith((b"PORTAL_IVA_GROUP:", b"PORTAL_IVA_CASE:")):
                        return
                if len(captured) + len(line) <= _PIPE_CAPTURE_LIMIT:
                    captured.extend(line)

            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    if pending and not discarding:
                        await handle_line(bytes(pending))
                    return bytes(captured)
                offset = 0
                while offset < len(chunk):
                    end = chunk.find(b"\n", offset)
                    if discarding:
                        if end < 0:
                            break
                        discarding = False
                        offset = end + 1
                        continue
                    if end < 0:
                        pending.extend(chunk[offset:])
                        if len(pending) > _PIPE_CAPTURE_LIMIT:
                            pending.clear()
                            discarding = True
                        break
                    pending.extend(chunk[offset:end + 1])
                    if len(pending) <= _PIPE_CAPTURE_LIMIT:
                        await handle_line(bytes(pending))
                    pending.clear()
                    offset = end + 1

        stdout_task = asyncio.create_task(consume(proc.stdout))
        stderr_task = asyncio.create_task(consume(proc.stderr))
        wait_task = asyncio.create_task(proc.wait())
        try:
            stdout, stderr, _ = await asyncio.gather(stdout_task, stderr_task, wait_task)
            return stdout, stderr
        except BaseException:
            if getattr(proc, "returncode", None) is None:
                await self._terminate_process(proc)
            for task in (stdout_task, stderr_task, wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
            raise

    def _deliverables(self, slug: str, cuit: str, period: str, result: dict[str, Any]) -> list[tuple[Path, int, str]]:
        if not slug or not cuit or not period:
            raise RuntimeError("PORTAL_IVA_STATE_INVALID")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or not re.fullmatch(r"\d{11}", cuit):
            raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
        year, month = period.split("-")
        root = self.clients_root / slug / cuit / "arca" / year / month / "consultas"
        try:
            clients_root_real = self.clients_root.resolve(strict=True)
            cursor = self.clients_root
            for part in (slug, cuit, "arca", year, month, "consultas"):
                cursor = cursor / part
                if cursor.is_symlink():
                    raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
            root_real = root.resolve(strict=True)
            if not root_real.is_relative_to(clients_root_real):
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
        except OSError as exc:
            raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID") from exc
        files = result.get("archivos")
        if not isinstance(files, list):
            raise RuntimeError("PORTAL_IVA_OUTPUT_INCOMPLETE")
        output: list[tuple[Path, int, str]] = []
        expected = {"ventas", "compras"}
        for item in files:
            if not isinstance(item, dict) or str(item.get("libro", "")) not in expected:
                raise RuntimeError("PORTAL_IVA_OUTPUT_INCOMPLETE")
            name = str(item.get("entregable_name", ""))
            if not name or Path(name).name != name or Path(name).suffix.lower() != ".csv":
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
            path = root / name
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID") from exc
            if path.is_symlink() or not path.is_file() or resolved.parent != root_real:
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
            output.append((path, int(item.get("filas", 0) or 0), str(item["libro"])))
        if {label for _, _, label in output} != expected or len(output) != 2:
            raise RuntimeError("PORTAL_IVA_OUTPUT_INCOMPLETE")
        return sorted(output, key=lambda item: {"ventas": 0, "compras": 1}[item[2]])

    @staticmethod
    def _stage_delivery_files(deliverables: list[tuple[Path, int, str]]) -> tuple[Path, list[tuple[Path, int, str]]]:
        """Copy validated files through no-follow descriptors before Telegram opens them."""
        staging = Path(tempfile.mkdtemp(prefix="portal-iva-telegram-"))
        os.chmod(staging, 0o700)
        copied: list[tuple[Path, int, str]] = []
        try:
            for source, rows, label in deliverables:
                directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    file_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
                try:
                    if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                        raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                    destination = staging / source.name
                    with os.fdopen(file_fd, "rb") as src, destination.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    destination.chmod(0o600)
                    copied.append((destination, rows, label))
                except Exception:
                    try:
                        os.close(file_fd)
                    except OSError:
                        pass
                    raise
            return staging, copied
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        code = str(exc)
        if re.fullmatch(r"PORTAL_IVA_[A-Z0-9_]+", code):
            return code
        return type(exc).__name__

    @staticmethod
    def _completion_message(deliverables: list[tuple[Path, int, str]]) -> str:
        lines = ["Portal IVA completado."]
        for _, rows, label in deliverables:
            title = label.title()
            if rows == 0:
                lines.append(f"{title}: sin comprobantes para el período.")
            elif rows == 1:
                lines.append(f"{title}: 1 comprobante; archivo enviado.")
            else:
                lines.append(f"{title}: {rows} comprobantes; archivo enviado.")
        return "\n".join(lines)

    @staticmethod
    def _error_message(result: dict[str, Any] | None, fallback: str, operation: str) -> str:
        prefix = "Generar CSV de período nuevo" if operation == "generar" else "Descargar CSV presentados"
        reason = str((result or {}).get("motivo", ""))
        if reason.startswith("CREDENCIAL_") or "CREDENTIAL" in reason:
            return f"{prefix}: ARCA rechazó la credencial."
        if reason == "CAPTCHA_REINTENTOS_AGOTADOS":
            return f"{prefix}: ARCA rechazó tres respuestas de captcha."
        if reason in {"PORTAL_IVA_CAPTCHA_TIMEOUT", "CAPTCHA_RESPUESTA_AUSENTE"}:
            return f"{prefix}: venció el tiempo para responder el captcha."
        missing_presented = re.fullmatch(r"PERIODO_NO_PRESENTADO_(\d{4})-(0[1-9]|1[0-2])", reason)
        if missing_presented and operation == "descargar-presentados":
            year, month = missing_presented.groups()
            return (
                f"{prefix}: el período {month}/{year} no figura como presentado en ARCA. "
                "Si todavía no fue presentado, usá “Generar CSV de período nuevo”."
            )
        own_relation = re.fullmatch(r"TITULAR_ES_EL_REPRESENTADO_(\d{11})", reason)
        if own_relation:
            return (
                f"{prefix}: el contribuyente elegido es el titular de la clave de ARCA, "
                "así que no hay representación que usar. Portal IVA no deja representarse "
                "a uno mismo, y la portada avisa que ese CUIT no tiene activa la "
                "caracterización de IVA. Elegí un contribuyente representado por ese titular."
            )
        not_listed = re.fullmatch(r"REPRESENTADO_NO_LISTADO_(\d{11})", reason)
        if not_listed:
            return (
                f"{prefix}: ARCA no lista a ese contribuyente entre los representados "
                "por el titular de la clave. Revisá la representación en ARCA."
            )
        unavailable = re.fullmatch(
            r"PERIODO_NO_DISPONIBLE_(\d{4})-(0[1-9]|1[0-2])_OFRECE_((?:\d{6})(?:_\d{6})*)", reason)
        if unavailable:
            year, month, offered = unavailable.groups()
            periods = ", ".join(
                f"{value[4:]}/{value[:4]}" for value in offered.split("_"))
            return (
                f"{prefix}: el período {month}/{year} no está disponible. "
                f"ARCA ofrece {periods} para declaración nueva."
            )
        if reason.startswith(("PERIODO_NO_DISPONIBLE", "PERIODO_NO_PRESENTADO")):
            return f"{prefix}: el período no está disponible para esta operación."
        if reason:
            return f"{prefix} no se completó. La evidencia quedó preservada para revisión."
        return fallback

    @staticmethod
    def _period_range(start: str, end: str) -> list[str]:
        year, month = map(int, start.split("-")); stop = tuple(map(int, end.split("-")))
        result = []
        while (year, month) <= stop:
            result.append(f"{year:04d}-{month:02d}")
            month += 1
            if month == 13: year, month = year + 1, 1
        return result

    def _batch_deliverables(self, result: dict[str, Any]) -> list[tuple[Path, int, str]]:
        root = self.clients_root.resolve(strict=True)
        output: list[tuple[Path, int, str]] = []
        for case in result.get("casos", []):
            if not isinstance(case, dict) or not case.get("ok"):
                continue
            for record in case.get("archivos", []):
                path = Path(str(record.get("entregable_path", "")))
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root):
                    raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("entregable_sha256"):
                    raise RuntimeError("PORTAL_IVA_DELIVERY_HASH_INVALID")
                output.append((path, int(record.get("filas", 0)), f"{case['cliente']} · {case['periodo']} · {record['libro']}"))
            f2083 = case.get("f2083")
            if isinstance(f2083, dict) and f2083.get("estado") == "disponible":
                path = Path(str(f2083.get("ruta", "")))
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root) or path.suffix.lower() != ".pdf":
                    raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                if hashlib.sha256(path.read_bytes()).hexdigest() != f2083.get("sha256"):
                    raise RuntimeError("PORTAL_IVA_DELIVERY_HASH_INVALID")
                output.append((path, 0, f"{case['cliente']} · {case['periodo']} · F.2083"))
        for workbook in result.get("xlsx", []):
            path = Path(str(workbook.get("ruta", "")))
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root) or path.suffix.lower() != ".xlsx":
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
            if hashlib.sha256(path.read_bytes()).hexdigest() != workbook.get("sha256"):
                raise RuntimeError("PORTAL_IVA_DELIVERY_HASH_INVALID")
            output.append((path, 0, f"{workbook['cliente']} · consolidado"))
        return output

    def _stage_batch_archive(self, deliverables: list[tuple[Path, int, str]],
                             name: str = "libros-iva-lote.zip",
                             evidence: list[tuple[Path, str]] | None = None) -> tuple[Path, Path]:
        """Send one ZIP while leaving each validated original in Documentos."""
        if Path(name).name != name or not name.endswith(".zip"):
            raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
        root = self.clients_root.resolve(strict=True)
        staging = Path(tempfile.mkdtemp(prefix="portal-iva-telegram-"))
        archive = staging / name
        seen: set[str] = set()
        try:
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as target, zipfile.ZipFile(
                target, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as package:
                def add_file(source: Path, archive_name: str) -> None:
                    if archive_name in seen:
                        raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                    seen.add(archive_name)
                    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(source_fd, "rb") as original:
                        if not stat.S_ISREG(os.fstat(original.fileno()).st_mode):
                            raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                        with package.open(archive_name, "w") as member:
                            shutil.copyfileobj(original, member)

                for source, _, _ in deliverables:
                    relative = source.relative_to(self.clients_root)
                    if len(relative.parts) < 3 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", relative.parts[0]) \
                            or not re.fullmatch(r"\d{11}", relative.parts[1]):
                        raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                    cursor = self.clients_root
                    for part in relative.parts:
                        cursor = cursor / part
                        if cursor.is_symlink():
                            raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                    if not source.resolve(strict=True).is_relative_to(root):
                        raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                    archive_name = "/".join((relative.parts[0], *relative.parts[2:]))
                    add_file(source, archive_name)
                evidence_root = self.captcha_root.resolve(strict=True) if evidence else None
                for source, evidence_name in evidence or []:
                    if Path(evidence_name).name != evidence_name or not re.fullmatch(
                        r"captura-arca-[a-z0-9-]+-\d{4}-(0[1-9]|1[0-2])\.png", evidence_name
                    ) or source.is_symlink() or not source.resolve(strict=True).is_relative_to(evidence_root):
                        raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
                    add_file(source, f"evidencia/{evidence_name}")
            return staging, archive
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _stage_batch_archives(self, deliverables: list[tuple[Path, int, str]],
                              name: str, evidence: list[tuple[Path, str]]) -> tuple[list[tuple[Path, Path]], list[str]]:
        """Pack by client, then period; never hand Telegram a ZIP over 45 MB."""
        if Path(name).name != name or not name.endswith(".zip"):
            raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
        groups: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        skipped: list[str] = []
        for record in deliverables:
            source = record[0]
            relative = source.relative_to(self.clients_root)
            if len(relative.parts) < 5:
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
            client = relative.parts[0]
            period = "/".join(relative.parts[3:5])
            if source.stat().st_size > _TELEGRAM_FILE_LIMIT:
                skipped.append(f"{client}: {source.name}")
                continue
            groups.setdefault((client, period), []).append(("file", record))
        for record in evidence:
            source, evidence_name = record
            match = re.fullmatch(r"captura-arca-([a-z0-9-]+)-(\d{4})-(\d{2})\.png", evidence_name)
            if not match:
                raise RuntimeError("PORTAL_IVA_DELIVERY_PATH_INVALID")
            if source.stat().st_size > _TELEGRAM_FILE_LIMIT:
                skipped.append(f"{match.group(1)}: {evidence_name}")
                continue
            groups.setdefault((match.group(1), f"{match.group(2)}/{match.group(3)}"), []).append(("evidence", record))

        batches: list[list[tuple[str, Any]]] = []
        current: list[tuple[str, Any]] = []
        current_size = 0
        current_client = None
        for (client, _period), records in sorted(groups.items()):
            group_size = sum(item[0].stat().st_size for _, item in records)
            if current and (client != current_client or current_size + group_size > _ZIP_PACK_BUDGET):
                batches.append(current)
                current, current_size = [], 0
            for record in records:
                size = record[1][0].stat().st_size
                if current and current_size + size > _ZIP_PACK_BUDGET:
                    batches.append(current)
                    current, current_size = [], 0
                current.append(record)
                current_size += size
                current_client = client
        if current:
            batches.append(current)

        archives: list[tuple[Path, Path]] = []
        def stage(records: list[tuple[str, Any]]) -> None:
            files = [item for kind, item in records if kind == "file"]
            captures = [item for kind, item in records if kind == "evidence"]
            staging, archive = self._stage_batch_archive(files, name, captures)
            if archive.stat().st_size <= _TELEGRAM_ZIP_LIMIT:
                archives.append((staging, archive))
                return
            shutil.rmtree(staging)
            if len(records) == 1:
                skipped.append(f"{records[0][1][0].name}")
                return
            midpoint = len(records) // 2
            stage(records[:midpoint])
            stage(records[midpoint:])
        try:
            for batch in batches:
                stage(batch)
            if len(archives) > 1:
                count = len(archives)
                renamed = []
                for number, (staging, archive) in enumerate(archives, 1):
                    target = staging / f"{name[:-4]}-parte-{number}-de-{count}.zip"
                    archive.rename(target)
                    renamed.append((staging, target))
                archives = renamed
            return archives, skipped
        except Exception:
            for staging, _ in archives:
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def _batch_evidence(self, result: dict[str, Any]) -> list[tuple[Path, str]]:
        root = self.captcha_root.resolve(strict=True)
        output: list[tuple[Path, str]] = []
        for case in result.get("casos", []):
            if case.get("motivo_codigo") != "respuesta_arca_inesperada":
                continue
            path = Path(str(case.get("evidencia_captura", "")))
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if path.is_symlink() or not resolved.is_relative_to(root) or resolved.suffix.lower() != ".png":
                continue
            if not resolved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
                continue
            output.append((resolved, f"captura-arca-{case.get('cliente')}-{case.get('periodo')}.png"))
        return output

    async def _run_batch(self, adapter, chat_id: Any, thread_id: Any, key: str, state: FlowState) -> None:
        proc = None; staging = None; history_run = None; result_dir = None
        archive_staging: list[Path] = []
        skipped_delivery: list[str] = []
        result: dict[str, Any] | None = None
        database_authoritative = False
        history_items: dict[tuple[int, str], int] = {}
        history_finished: set[int] = set()
        verified_clients: list[dict[str, Any]] = []
        periods: list[str] = []

        async def finish_remaining(item_state: str, code: str, text: str) -> None:
            if self.history is None or history_run is None:
                return
            for item in history_items.values():
                if item in history_finished:
                    continue
                await self.history.finish_item(
                    history_run, item, state=item_state, reason_code=code,
                    reason_text=text, effects=[], delivered_count=0,
                )
                history_finished.add(item)
            await self.history.close(history_run)
        try:
            # El permiso se vuelve a comprobar justo antes de ejecutar: entre
            # la apertura del flujo y la confirmación del rango pudo retirarse.
            await asyncio.to_thread(
                self._scope().require_permission,
                state.user_id,
                "operaciones.ejecutar",
            )
            if not state.selected_clients or not state.period_from or not state.period_to:
                raise RuntimeError("PORTAL_IVA_STATE_INVALID")
            for selected in state.selected_clients:
                rows = await asyncio.to_thread(self._by_id, int(selected["id"]), state.user_id)
                if len(rows) != 1 or rows[0].get("relation_revision") != selected.get("relation_revision"):
                    raise RuntimeError("PORTAL_IVA_SELECTION_STALE")
                verified_clients.append(rows[0])
            if len({int(client["study_id"]) for client in verified_clients}) != 1:
                raise RuntimeError("PORTAL_IVA_SELECTION_STALE")
            # El comando usa exclusivamente el binding revalidado en este instante,
            # no la selección conservada desde mensajes anteriores.
            state.selected_clients = verified_clients
            periods = self._period_range(state.period_from, state.period_to)
            state.batch_months = len(periods)
            state.progress_label = "Conectando con ARCA para consultar los Libros IVA…"
            await self._edit_progress(state, state.progress_label, keyboard=self._cancel_keyboard(state.nonce))
            if self.history is not None:
                history_run = await self.history.start(
                    telegram_id=int(state.user_id), operation="portal_iva_lote_presentados",
                    key_material=f"portal-iva-lote:{chat_id}:{thread_id or ''}:{state.nonce}",
                    reference=f"{len(verified_clients)} contribuyentes · {state.period_from} a {state.period_to}",
                    lease_seconds=_RUN_TIMEOUT_SECONDS + 120,
                )
                refs = [f"{client['slug']} · {period}" for client in verified_clients for period in periods]
                items = await self.history.prepare_items(history_run, refs)
                pairs = [(client, period) for client in verified_clients for period in periods]
                history_items = {(int(client["id"]), period): int(item["id_item"])
                                 for (client, period), item in zip(pairs, items, strict=True)}
            result_dir, result_file = self._new_batch_result_target()
            proc = await asyncio.create_subprocess_exec(*self._batch_command(
                state, history_run.run_id if history_run else None, result_file), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=_PIPE_STREAM_LIMIT)
            self.processes[key] = proc
            async def on_batch_event(kind, payload):
                await self._batch_progress_event(state, kind, payload)
            await asyncio.wait_for(self._communicate_with_progress(
                proc, state, adapter=adapter, chat_id=chat_id, thread_id=thread_id,
                batch_event=on_batch_event), timeout=_RUN_TIMEOUT_SECONDS)
            result = self._read_batch_result(result_file)
            database_authoritative = (
                type(result.get("id_lote")) is int and result["id_lote"] > 0
            )
            if proc.returncode or not result.get("ok"):
                raise RuntimeError("PORTAL_IVA_BATCH_FAILED")
            source = self._batch_deliverables(result)
            evidence = self._batch_evidence(result)
            if source or evidence:
                state.stage = "delivering"
                if len(source) + len(evidence) > 2:
                    archive_name = f"libros-iva-{state.period_from.replace('-', '')}-a-{state.period_to.replace('-', '')}.zip"
                    archives, skipped_delivery = self._stage_batch_archives(source, archive_name, evidence)
                    archive_staging.extend(folder for folder, _ in archives)
                    await self._edit_progress(state, f"Lote completado. Enviando {len(archives)} ZIP(s)…")
                    for part_number, (_, archive) in enumerate(archives, 1):
                        caption = (f"{len(source) + len(evidence) - len(skipped_delivery)} archivos del lote en un ZIP. "
                                   if len(archives) == 1 else
                                   f"Resultados del lote, parte {part_number} de {len(archives)}. ")
                        delivery = await adapter.send_document(
                            chat_id=str(chat_id), file_path=str(archive), file_name=archive.name,
                            caption=caption + "Los archivos también quedaron sueltos en Documentos.",
                            metadata={"thread_id": thread_id} if thread_id is not None else None)
                        if not delivery.success:
                            raise RuntimeError("PORTAL_IVA_DELIVERY_FAILED")
                else:
                    if source:
                        sendable = []
                        for record in source:
                            if record[0].stat().st_size > _TELEGRAM_FILE_LIMIT:
                                skipped_delivery.append(f"{record[2]}: {record[0].name}")
                            else:
                                sendable.append(record)
                        if sendable:
                            staging, deliverables = self._stage_delivery_files(sendable)
                        else:
                            deliverables = []
                        await self._edit_progress(state, "Lote completado. Entregando resultados…")
                        for path, rows, label in deliverables:
                            delivery = await adapter.send_document(chat_id=str(chat_id), file_path=str(path), file_name=path.name,
                                caption=label + (f" · {rows} fila(s)" if rows else ""),
                                metadata={"thread_id": thread_id} if thread_id is not None else None)
                            if not delivery.success:
                                raise RuntimeError("PORTAL_IVA_DELIVERY_FAILED")
                    for path, name in evidence:
                        if path.stat().st_size > _TELEGRAM_FILE_LIMIT:
                            skipped_delivery.append(name)
                            continue
                        delivery = await adapter.send_document(
                            chat_id=str(chat_id), file_path=str(path), file_name=name,
                            caption="ARCA respondió algo que no esperábamos. Captura para revisión.",
                            metadata={"thread_id": thread_id} if thread_id is not None else None)
                        if not delivery.success:
                            raise RuntimeError("PORTAL_IVA_EVIDENCE_DELIVERY_FAILED")
            if self.history is not None and history_run is not None and not database_authoritative:
                for case in result.get("casos", []):
                    item = history_items[(int(case["id_contribuyente"]), str(case["periodo"]))]
                    success = bool(case.get("ok"))
                    case_state = "completado" if success else ("rechazado" if case.get("estado") == "no_presentado" else "fallido")
                    await self.history.finish_item(history_run, item,
                        state=case_state, reason_code="ok" if success else self._history_reason_code(case.get("motivo_codigo") or case.get("motivo")),
                        reason_text="Libros IVA descargados." if success else str(case.get("motivo_texto", "ARCA respondió algo que no esperábamos."))[:500],
                        effects=[{"tipo": "consulta_read_only", "realizado": True}],
                        contributor_id=int(case["id_contribuyente"]), period=str(case["periodo"]),
                        delivered_count=sum(int(x.get("filas", 0)) for x in case.get("archivos", [])))
                    history_finished.add(item)
                await self.history.close(history_run)
            summary = result.get("resumen", {})
            pending = [case for case in result.get("casos", []) if not case.get("ok")]
            detail = "\n".join(
                f"• {case.get('cliente')} · {case.get('periodo')}: {case.get('motivo_texto', 'ARCA respondió algo que no esperábamos.')}"
                + (f" {case.get('accion_sugerida')}" if case.get("accion_sugerida") else "")
                for case in pending[:8]
            )
            suffix = f"\n{detail}" if detail else ""
            delivered = sum(value == "descargado" for files in state.batch_files.values() for value in files.values())
            expected = (len(verified_clients) * len(periods) * 2
                        - sum(value == "sin_libro" for files in state.batch_files.values()
                              for value in files.values()))
            errors = sum(value == "error" for files in state.batch_files.values()
                         for value in files.values())
            omitted = (f"\nNo se enviaron {len(skipped_delivery)} archivo(s) que exceden el límite de Telegram; "
                       "quedaron en Documentos y en el panel." if skipped_delivery else "")
            if not source and not evidence:
                delivery_text = "Sin archivos para enviar."
            elif len(skipped_delivery) == len(source) + len(evidence):
                delivery_text = "Sin archivos enviados por Telegram."
            else:
                delivery_text = "Resultados enviados."
            await self._edit_progress(state,
                f"Lote completado: {summary.get('completados', 0)} de {summary.get('total', 0)} casos; "
                f"{delivered}/{expected} libros descargados, {errors} con error. "
                f"{delivery_text}{omitted}{suffix}")
        except asyncio.CancelledError:
            if proc and proc.returncode is None: await self._terminate_process(proc)
            if not database_authoritative and history_run is not None and verified_clients and periods:
                try:
                    await asyncio.to_thread(self._register_terminal_batch, history_run.run_id, verified_clients,
                                            periods, "cancelado", "cancelado_por_usuario")
                    database_authoritative = True
                except Exception:
                    logger.exception("[PORTAL-IVA] lote cancelado no pudo registrarse para aviso")
            if not database_authoritative:
                try:
                    await finish_remaining("cancelado", "cancelado_por_usuario", "El usuario canceló el lote.")
                except Exception:
                    logger.exception("[PORTAL-IVA] lote cancelado no pudo cerrar historial")
            raise
        except Exception:
            logger.exception("[PORTAL-IVA] lote no completado")
            if not database_authoritative and history_run is not None and verified_clients and periods:
                try:
                    await asyncio.to_thread(self._register_terminal_batch, history_run.run_id, verified_clients,
                                            periods, "fallido", "lote_no_completado")
                    database_authoritative = True
                except Exception:
                    logger.exception("[PORTAL-IVA] lote fallido no pudo registrarse para aviso")
            if not database_authoritative:
                try:
                    await finish_remaining("fallido", "lote_no_completado", "El lote no pudo completarse.")
                except Exception:
                    logger.exception("[PORTAL-IVA] lote fallido no pudo cerrar historial")
            await self._edit_progress(state, "No pude completar el lote. El avance y la evidencia quedaron preservados.")
        finally:
            if staging: shutil.rmtree(staging, ignore_errors=True)
            for folder in archive_staging:
                shutil.rmtree(folder, ignore_errors=True)
            if result_dir: shutil.rmtree(result_dir, ignore_errors=True)
            self.processes.pop(key, None)

    async def _run(self, adapter, chat_id: Any, thread_id: Any, key: str, state: FlowState) -> None:
        ticker = None
        result: dict[str, Any] | None = None
        staging: Path | None = None
        proc: asyncio.subprocess.Process | None = None
        execution_key: str | None = None
        execution_lock_acquired = False
        history_run: RunHandle | None = None
        history_items: dict[str, int] = {}
        history_finished: set[int] = set()

        async def finish_remaining(item_state: str, code: str, text: str) -> None:
            if history_run is None or self.history is None:
                return
            effects = self._history_effects(state.operation, prepared=False)
            for item_id in history_items.values():
                if item_id not in history_finished:
                    await self.history.finish_item(
                        history_run, item_id, state=item_state, reason_code=code,
                        reason_text=text, effects=effects,
                        contributor_id=state.contributor_id, period=state.period,
                    )
                    history_finished.add(item_id)
            await self.history.close(history_run)
        try:
            if state.cancelled:
                return
            if state.contributor_id is None or not state.slug or not state.cuit or not state.period:
                raise RuntimeError("PORTAL_IVA_STATE_INVALID")
            contributor_id = state.contributor_id
            slug, cuit, period = state.slug, state.cuit, state.period
            operation_code = (
                "portal_iva_generar_csv"
                if state.operation == "generar"
                else "portal_iva_descargar_presentados"
            )
            if self.history is not None:
                history_run = await self.history.start(
                    telegram_id=int(state.user_id), operation=operation_code,
                    key_material=f"portal-iva:{chat_id}:{thread_id or ''}:{state.nonce}:{state.operation}",
                    reference=f"{slug}-{period}", lease_seconds=_RUN_TIMEOUT_SECONDS + 120,
                )
                prepared_items = await self.history.prepare_items(
                    history_run, ["Libro IVA Ventas", "Libro IVA Compras"],
                )
                history_items = {
                    label: int(item["id_item"])
                    for label, item in zip(("ventas", "compras"), prepared_items, strict=True)
                }
            verified = await asyncio.to_thread(self._by_id, state.contributor_id, state.user_id)
            if state.cancelled:
                return
            if (len(verified) != 1 or verified[0].get("slug") != slug
                    or verified[0].get("cuit") != cuit
                    or state.scope_item is None
                    or verified[0].get("relation_id") != state.scope_item.get("relation_id")
                    or verified[0].get("relation_revision") != state.scope_item.get("relation_revision")):
                raise RuntimeError("PORTAL_IVA_SELECTION_STALE")
            execution_key = f"{contributor_id}:{period}"
            owner = self.execution_locks.get(execution_key)
            if owner is not None and owner != key:
                raise RuntimeError("PORTAL_IVA_EXECUTION_ALREADY_RUNNING")
            self._acquire_execution_lock(execution_key)
            execution_lock_acquired = True
            self.execution_locks[execution_key] = key
            command = self._command(slug, period, state.operation)
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
                limit=_PIPE_STREAM_LIMIT,
            )
            self.processes[key] = proc
            if state.cancelled:
                await self._terminate_process(proc)
                return
            ticker = asyncio.create_task(self._ticker(state, started))
            try:
                raw, _ = await asyncio.wait_for(
                    self._communicate_with_progress(
                        proc, state, adapter=adapter, chat_id=chat_id, thread_id=thread_id,
                    ),
                    timeout=_RUN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await self._terminate_process(proc)
                raise RuntimeError("PORTAL_IVA_TIMEOUT")
            if state.cancelled:
                return
            result = self._parse_result(raw)
            if proc.returncode != 0 or not result.get("ok"):
                failure_message = self._error_message(
                    result, f"{state.operation} no se completó.", state.operation,
                )
                await self._edit_progress(state, failure_message)
                # A terminal outcome must create a fresh, visible notification.
                # Editing an older progress message alone is easy to miss in Telegram.
                await self._send(adapter, chat_id, failure_message, thread_id)
                await finish_remaining(
                    "fallido", self._history_reason_code(result.get("motivo")), failure_message,
                )
                return
            if result.get("etapa") != "completado":
                raise RuntimeError("PORTAL_IVA_OUTPUT_INCOMPLETE")
            await verify_representation(
                state.user_id, verified[0], "ARCA",
                str(result.get("representado_verificado", "")),
            )
            state.progress_label = "Validando archivos…"
            await self._edit_progress(state, state.progress_label, keyboard=self._cancel_keyboard(state.nonce))
            source_deliverables = self._deliverables(slug, cuit, period, result)
            deliverables = source_deliverables
            if state.cancelled:
                return
            staging, deliverables = self._stage_delivery_files(deliverables)
            if state.cancelled:
                return
            state.stage = "delivering"
            await self._edit_progress(state, "Portal IVA completado. Entregando Ventas y Compras…")
            for path, rows, label in deliverables:
                delivery = await adapter.send_document(
                    chat_id=str(chat_id), file_path=str(path), file_name=path.name,
                    caption=f"Portal IVA {label.title()}: {rows} fila(s).",
                    metadata={"thread_id": thread_id} if thread_id is not None else None,
                )
                if not delivery.success:
                    raise RuntimeError("PORTAL_IVA_DELIVERY_FAILED")
                delivered_filename = getattr(delivery, "delivered_filename", None)
                if delivered_filename != path.name:
                    logger.error(
                        "[PORTAL-IVA] stage=delivery status=DELIVERY_FILENAME_MISMATCH "
                        "expected=%r delivered=%r",
                        path.name,
                        delivered_filename,
                    )
            warnings = result.get("advertencias") if isinstance(result.get("advertencias"), list) else []
            safe_warnings = [
                str(code) for code in warnings
                if re.fullmatch(r"[A-Z][A-Za-z0-9_]{0,127}", str(code))
            ]
            if safe_warnings:
                logger.warning(
                    "[PORTAL-IVA] completed_with_warnings codes=%s",
                    ",".join(safe_warnings),
                )
            if self.history is not None and history_run is not None:
                source_by_label = {label: (path, rows) for path, rows, label in source_deliverables}
                for label in ("ventas", "compras"):
                    path, rows = source_by_label[label]
                    item_state, reason_code, reason_text = self._history_outcome(label, safe_warnings)
                    await self.history.finish_item(
                        history_run, history_items[label],
                        state=item_state, reason_code=reason_code, reason_text=reason_text,
                        effects=self._history_effects(state.operation, prepared=True),
                        contributor_id=contributor_id, period=period, delivered_count=rows,
                        output=path, output_relative=str(path.relative_to(self.clients_root)),
                    )
                    history_finished.add(history_items[label])
                await self.history.close(history_run)
            await self._edit_progress(state, self._completion_message(deliverables))
        except asyncio.CancelledError:
            state.cancelled = True
            if proc is not None and proc.returncode is None:
                await self._terminate_process(proc)
            try:
                await asyncio.shield(finish_remaining(
                    "cancelado", "cancelado_por_usuario", "La operación fue cancelada por el usuario.",
                ))
            except Exception:
                logger.exception("[PORTAL-IVA] history cancellation failed")
            raise
        except Exception as exc:
            logger.error(
                "[PORTAL-IVA] run failed error_type=%s error_code=%s",
                type(exc).__name__,
                self._safe_error_code(exc),
            )
            if not state.cancelled:
                error_result = result if result is not None else {"motivo": str(exc)}
                await self._edit_progress(
                    state,
                    self._error_message(
                        error_result, f"{state.operation} no se completó.", state.operation,
                    ),
                )
                try:
                    await finish_remaining(
                        "fallido", self._history_reason_code(str(exc)),
                        self._error_message(error_result, f"{state.operation} no se completó.", state.operation),
                    )
                except Exception:
                    logger.exception("[PORTAL-IVA] history failure close failed")
        finally:
            if ticker is not None:
                ticker.cancel()
            self.processes.pop(key, None)
            if execution_key and self.execution_locks.get(execution_key) == key:
                self.execution_locks.pop(execution_key, None)
            if execution_lock_acquired and execution_key:
                self._release_execution_lock(execution_key)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _history_reason_code(reason: object) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(reason).lower()).strip("_")
        return normalized[:80] if len(normalized) >= 3 else "error_portal_iva"

    @staticmethod
    def _history_effects(operation: str, *, prepared: bool) -> list[dict[str, Any]]:
        if operation == "generar":
            return [
                {"codigo": "preparar", "realizado": prepared},
                {"codigo": "presentar", "realizado": False, "detalle": "Esta operación no presenta la DDJJ."},
                {"codigo": "pagar", "realizado": False, "detalle": "Esta operación no realiza pagos."},
            ]
        return [
            {"codigo": "consultar", "realizado": prepared},
            {"codigo": "preparar", "realizado": False, "detalle": "Sólo descarga libros ya presentados."},
            {"codigo": "presentar", "realizado": False, "detalle": "No modifica la presentación."},
        ]

    @staticmethod
    def _history_outcome(label: str, warnings: list[str]) -> tuple[str, str, str]:
        if f"IMPORTACION_NUMEROS_NO_PARSEADOS_{label}" in warnings:
            return (
                "incompleto", f"numeros_no_importados_{label}",
                f"Hay comprobantes de {label} que no se pudieron importar; revisalos antes de presentar.",
            )
        return (
            "completado", "archivo_entregado",
            f"El Libro IVA {label.title()} fue entregado correctamente.",
        )

    def _release(self, key: str, task: asyncio.Task) -> None:
        self.tasks.pop(key, None)
        state = self.states.pop(key, None)
        if state and state.captcha_response is not None and not state.captcha_response.done():
            state.captcha_response.set_result(None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[PORTAL-IVA] background task failed")
