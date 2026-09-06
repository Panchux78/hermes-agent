"""Estado y consultas del flujo inline de DDJJ AGIP."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

try:
    import fcntl
except ImportError:  # Windows gateway: keep Telegram importable, hide this Linux-only feature.
    fcntl = None

logger = logging.getLogger(__name__)
_CLIENTS_ROOT = "/home/pancho/clientes"
_LOCK_ROOT = Path("/home/pancho/.local/state/contabot/agip-ddjj/telegram-locks")
_FAILURE_ROOT = Path("/home/pancho/.local/state/contabot/agip-ddjj/failures")
_STATE_TTL_SECONDS = 600
_AGIP_CLAVE_CIUDAD_ENTITY = "AGIP - Clave Ciudad"
_RUN_TIMEOUT_SECONDS = 1800
_DELIVERY_XLSX = re.compile(
    rf"^(?:{re.escape(_CLIENTS_ROOT)}/[a-z0-9]+(?:-[a-z0-9]+)*/(?P<cuit_annual>\d{{11}})/agip/"
    r"(?P<year_annual>\d{4})/anual/consultas/(?P=cuit_annual)-ddjj-iibb-agip-"
    r"(?P=year_annual)(?:-v\d{2})?\.xlsx|"
    rf"{re.escape(_CLIENTS_ROOT)}/[a-z0-9]+(?:-[a-z0-9]+)*/(?P<cuit_monthly>\d{{11}})/agip/"
    r"(?P<year_monthly>\d{4})/(?P<month>0[1-9]|1[0-2])/consultas/(?P=cuit_monthly)-"
    r"ddjj-iibb-agip-(?P=year_monthly)-(?P=month)(?:-v\d{2})?\.xlsx)$"
)


def is_valid_delivery_path(path: str) -> bool:
    """Accept only a v5 AGIP consultation XLSX for the represented CUIT."""
    return bool(_DELIVERY_XLSX.fullmatch(path or ""))


def visible_cuit(cuit: str) -> str:
    return f"{cuit[:2]}-******-{cuit[-1:]}" if re.fullmatch(r"\d{11}", cuit or "") else "CUIT oculto"


def normalize_period(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}", value):
        return value
    if re.fullmatch(r"\d{6}", value):
        value = f"{value[:4]}-{value[4:]}"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = value.split("-")
        if 1 <= int(month) <= 12:
            return value
    m = re.fullmatch(r"\d{2}/(\d{2})/(\d{4})", value)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{m.group(1)}"
    raise ValueError("Ingresá AAAA, AAAAMM, AAAA-MM o DD/MM/AAAA.")


@dataclass
class FlowState:
    user_id: str
    nonce: str
    stage: str
    contributor_id: int | None = None
    represented_id: int | None = None
    progress_message: Any = None
    cancelled: bool = False
    created_at: float = field(default_factory=time.monotonic)


class AgipDdjjFlow:
    """Inline flow; credentials remain in PostgreSQL and never leave the host."""
    def __init__(self) -> None:
        self.states: dict[str, FlowState] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.execution_locks: dict[str, str] = {}
        self.execution_lock_fds: dict[str, int] = {}

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _sql_scalar(value: str) -> str:
        return base64.b64encode(value.encode()).decode()

    def _query(self, sql: str) -> list[dict[str, Any]]:
        run = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "--dbname=contabot", "-At", "-c", sql],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        if run.returncode:
            raise RuntimeError("No se pudo consultar la base canónica")
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def _search(self, term: str, contributor_id: int | None = None) -> list[dict[str, Any]]:
        encoded = self._sql_scalar(term.strip())
        base = f"convert_from(decode('{encoded}','base64'),'UTF8')"
        if contributor_id is None:
            scope = f"""
              FROM tbl_contribuyentes c
              JOIN tbl_accesos a ON a.id_contribuyente=c.id_contribuyente AND a.activo
              JOIN tbl_entidades e ON e.id_entidad=a.id_entidad AND e.nombre='{_AGIP_CLAVE_CIUDAD_ENTITY}'
            """
        else:
            scope = f"""
              FROM tbl_representaciones r
              JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='{_AGIP_CLAVE_CIUDAD_ENTITY}' AND r.activo
              JOIN tbl_contribuyentes c ON c.id_contribuyente=r.id_contribuyente_representado
              WHERE r.id_contribuyente_representante={int(contributor_id)} AND c.activo AND
            """
        where = f"(lower(c.nombre_legal) LIKE '%'||lower({base})||'%' OR c.slug=lower({base}) OR c.cuit=regexp_replace({base},'[^0-9]','','g'))"
        if contributor_id is None:
            where = "WHERE c.activo AND " + where
        sql = f"SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,'cuit',c.cuit,'slug',c.slug)::text {scope} {where} ORDER BY c.nombre_legal LIMIT 12;"
        return self._query(sql)

    async def _send(self, adapter, chat_id, text: str, keyboard=None, thread_id=None):
        kwargs = {"chat_id": chat_id, "text": text, "reply_markup": keyboard}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        return await adapter._bot.send_message(**kwargs)

    @staticmethod
    def _cancel_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancelar", callback_data=f"ad:cancel:{nonce}")]]
        )

    @staticmethod
    def _expired(state: FlowState) -> bool:
        return state.stage != "running" and time.monotonic() - state.created_at > _STATE_TTL_SECONDS

    def _acquire_execution_lock(self, key: str) -> None:
        if fcntl is None:
            raise RuntimeError("AGIP_RUNTIME_UNAVAILABLE")
        _LOCK_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(_LOCK_ROOT, 0o700)
        path = _LOCK_ROOT / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("AGIP_EXECUTION_ALREADY_RUNNING") from exc
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

    async def _finish(self, adapter, chat_id, thread_id, state: FlowState, text: str) -> None:
        if state.progress_message is not None:
            try:
                await state.progress_message.edit_text(text, reply_markup=None)
                return
            except Exception:
                logger.warning("[AGIP-DDJJ] progress edit failed")
        await self._send(adapter, chat_id, text, thread_id=thread_id)

    @staticmethod
    def _worker_error_message(result: dict[str, Any], evidence_preserved: bool = False) -> str:
        """Translate known business outcomes without exposing worker internals."""
        code = str(result.get("error_code", ""))
        period = str(result.get("period", ""))
        if code == "AGIP_DDJJ_NOT_FOUND":
            message = "Consulta AGIP no completada: no hay DDJJ para el período solicitado."
        elif code == "AGIP_ACCESS_UNAVAILABLE":
            message = "Consulta AGIP no completada: el acceso o la representación ya no está disponible."
        elif code == "AGIP_DDJJ_LIST_UNAVAILABLE":
            message = "Consulta AGIP no completada: AGIP no terminó de cargar el listado de DDJJ."
        elif code == "AGIP_ESICOL_UNAVAILABLE":
            message = (
                "Consulta AGIP no completada: Clave Ciudad no muestra e-SICOL "
                "habilitado para ese contribuyente. Revisá la delegación del servicio en AGIP."
            )
        elif code.startswith("AGIP_DDJJ_PDF_") and re.fullmatch(r"\d{4}-\d{2}", period):
            message = f"Consulta AGIP no completada: AGIP no entregó el PDF de {period[5:7]}/{period[:4]}."
        else:
            stage = str(result.get("stage", ""))
            stage_messages = {
                "start_browser": "Consulta AGIP no completada: no se pudo iniciar el navegador.",
                "load_login": "Consulta AGIP no completada: no se pudo abrir Clave Ciudad.",
                "submit_login": "Consulta AGIP no completada: no se pudo completar el acceso a Clave Ciudad.",
                "select_represented": "Consulta AGIP no completada: no se pudo seleccionar el contribuyente representado.",
                "open_esicol": "Consulta AGIP no completada: no se pudo abrir e-SICOL.",
                "load_ddjj_list": "Consulta AGIP no completada: AGIP no terminó de cargar el listado de DDJJ.",
                "build_xlsx": "Consulta AGIP no completada al obtener o procesar las DDJJ.",
            }
            message = stage_messages.get(stage, "Consulta AGIP no completada por un error técnico.")
        if evidence_preserved:
            message += " El diagnóstico quedó registrado para revisión."
        return message

    @staticmethod
    def _persist_failure(result: dict[str, Any], period: str, returncode: int | None) -> Path:
        """Persist only allowlisted diagnostic fields, atomically and mode 0600."""
        _FAILURE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _FAILURE_ROOT.is_symlink() or not _FAILURE_ROOT.is_dir():
            raise RuntimeError("AGIP_FAILURE_STORE_INVALID")
        os.chmod(_FAILURE_ROOT, 0o700)
        payload: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "error_code": str(result.get("error_code", "AGIP_WORKER_RESULT_INVALID")),
            "requested_period": period,
            "process_returncode": returncode,
        }
        if re.fullmatch(r"\d{4}-\d{2}", str(result.get("period", ""))):
            payload["failed_period"] = str(result["period"])
        if str(result.get("response_kind", "")) in {"html", "non_pdf"}:
            payload["response_kind"] = str(result["response_kind"])
        stage = str(result.get("stage", ""))
        if stage in {
            "validate_arguments", "load_access", "resolve_output", "start_browser",
            "load_login", "submit_login", "select_represented", "open_esicol",
            "load_ddjj_list", "build_xlsx", "complete",
        }:
            payload["stage"] = stage
        error_type = str(result.get("error_type", ""))
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_type):
            payload["error_type"] = error_type
        status = result.get("http_status")
        if isinstance(status, int) and 100 <= status <= 599:
            payload["http_status"] = status
        name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}.json"
        fd, temporary = tempfile.mkstemp(prefix=".failure-", dir=_FAILURE_ROOT)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            destination = _FAILURE_ROOT / name
            os.replace(temporary, destination)
            return destination
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise

    async def start(self, adapter, query, chat_id, thread_id, user_id) -> None:
        key = self._key(chat_id, thread_id, user_id)
        if key in self.tasks:
            await query.answer("Ya hay una consulta AGIP en curso.")
            return
        self.states[key] = FlowState(
            user_id=str(user_id), nonce=uuid.uuid4().hex[:10], stage="contributor"
        )
        await query.answer("Consulta DDJJ IIBB")
        await self._send(adapter, chat_id, "Ingresá nombre, CUIT o slug del contribuyente.", thread_id=thread_id)

    async def text(self, adapter, message) -> bool:
        chat_id, user_id = message.chat_id, message.from_user.id
        thread_id = getattr(message, "message_thread_id", None)
        key = self._key(chat_id, thread_id, user_id)
        state = self.states.get(key)
        if not state:
            return False
        if self._expired(state):
            self.states.pop(key, None)
            await self._send(adapter, chat_id, "La solicitud venció. Iniciá una consulta nueva.", thread_id=thread_id)
            return True
        if state.stage == "running":
            await self._send(adapter, chat_id, "La consulta AGIP ya está en ejecución.", thread_id=thread_id)
            return True
        if state.stage == "period":
            try:
                period = normalize_period(message.text)
            except ValueError as exc:
                await self._send(adapter, chat_id, str(exc), thread_id=thread_id)
                return True
            if key in self.tasks:
                await self._send(adapter, chat_id, "Ya hay una consulta AGIP en curso.", thread_id=thread_id)
                return True
            state.stage = "running"
            state.progress_message = await self._send(
                adapter,
                chat_id,
                f"Consulta AGIP iniciada para {period}.",
                self._cancel_keyboard(state.nonce),
                thread_id,
            )
            task = asyncio.create_task(
                self._run_query(adapter, chat_id, thread_id, key, state, period)
            )
            self.tasks[key] = task
            task.add_done_callback(lambda done: self._release(key, done))
            return True
        candidates = self._search(message.text, state.contributor_id if state.stage == "represented" else None)
        if not candidates:
            label = "representado" if state.stage == "represented" else "contribuyente"
            await self._send(adapter, chat_id, f"No hay {label} AGIP activo que coincida. Probá con nombre, CUIT o slug.", thread_id=thread_id)
            return True
        if len(candidates) == 1:
            await self._select(adapter, chat_id, thread_id, user_id, state, candidates[0])
            return True
        kind = "r" if state.stage == "represented" else "c"
        rows = [[InlineKeyboardButton(f"{x['nombre']} — {visible_cuit(x['cuit'])}", callback_data=f"ad:{kind}:{state.nonce}:{x['id']}")] for x in candidates]
        await self._send(adapter, chat_id, "Elegí una opción:", InlineKeyboardMarkup(rows), thread_id)
        return True

    def _represented(self, contributor_id: int) -> list[dict[str, Any]]:
        """All AGIP represented contributors for the selected credential holder."""
        return self._query(
            "SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,"
            "'cuit',c.cuit,'slug',c.slug)::text "
            "FROM tbl_representaciones r "
            f"JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='{_AGIP_CLAVE_CIUDAD_ENTITY}' "
            "JOIN tbl_contribuyentes c ON c.id_contribuyente=r.id_contribuyente_representado "
            f"WHERE r.activo AND c.activo AND r.id_contribuyente_representante={int(contributor_id)} "
            "ORDER BY c.nombre_legal LIMIT 100;"
        )

    def _by_id(self, item_id: int, contributor_id: int | None) -> list[dict[str, Any]]:
        scope = ""
        if contributor_id is not None:
            scope = f"JOIN tbl_representaciones r ON r.id_contribuyente_representado=c.id_contribuyente AND r.id_contribuyente_representante={int(contributor_id)} AND r.activo JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='{_AGIP_CLAVE_CIUDAD_ENTITY}'"
        else:
            scope = f"JOIN tbl_accesos a ON a.id_contribuyente=c.id_contribuyente AND a.activo JOIN tbl_entidades e ON e.id_entidad=a.id_entidad AND e.nombre='{_AGIP_CLAVE_CIUDAD_ENTITY}'"
        return self._query(f"SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,'cuit',c.cuit,'slug',c.slug)::text FROM tbl_contribuyentes c {scope} WHERE c.activo AND c.id_contribuyente={int(item_id)};")

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id) -> bool:
        key = self._key(chat_id, thread_id, user_id)
        if data == "ad:start":
            await self.start(adapter, query, chat_id, thread_id, user_id)
            return True
        cancel = re.fullmatch(r"ad:cancel:([0-9a-f]{10})", data)
        if cancel:
            state = self.states.get(key)
            if not state or state.nonce != cancel.group(1) or state.stage != "running":
                await query.answer("Esta consulta ya no está activa.")
                return True
            await query.answer("Cancelando consulta AGIP…")
            state.cancelled = True
            proc = self.processes.get(key)
            if proc is not None and proc.returncode is None:
                await self._terminate_process(proc)
            await self._finish(
                adapter, chat_id, thread_id, state,
                "Consulta AGIP cancelada. No se entregó ningún archivo.",
            )
            return True
        same = re.fullmatch(r"ad:s:([0-9a-f]{10})", data)
        if same:
            state = self.states.get(key)
            if state and self._expired(state):
                self.states.pop(key, None)
                await query.answer("Esta selección venció. Iniciá una consulta nueva.")
                return True
            if not state or state.nonce != same.group(1) or state.stage != "represented" or state.contributor_id is None:
                await query.answer("Esta selección venció. Iniciá una consulta nueva.")
                return True
            found = self._by_id(state.contributor_id, state.contributor_id)
            if not found:
                await query.answer("El contribuyente no está habilitado como representado AGIP.")
                return True
            await query.answer("Mismo CUIT seleccionado")
            await self._select(adapter, chat_id, thread_id, user_id, state, found[0])
            return True
        m = re.fullmatch(r"ad:([cr]):([0-9a-f]{10}):(\d+)", data)
        if not m:
            return False
        state = self.states.get(key)
        if state and self._expired(state):
            self.states.pop(key, None)
            await query.answer("Esta selección venció. Iniciá una consulta nueva.")
            return True
        if not state or state.nonce != m.group(2) or (m.group(1) == "c") != (state.stage == "contributor"):
            await query.answer("Esta selección venció. Iniciá una consulta nueva.")
            return True
        found = self._by_id(int(m.group(3)), state.contributor_id if state.stage == "represented" else None)
        if not found:
            await query.answer("La opción ya no está disponible.")
            return True
        await query.answer("Seleccionado")
        await self._select(adapter, chat_id, thread_id, user_id, state, found[0])
        return True

    async def _select(self, adapter, chat_id, thread_id, user_id, state, row) -> None:
        if state.stage == "contributor":
            state.contributor_id = int(row['id']); state.stage = "represented"
            represented = self._represented(state.contributor_id)
            if not represented:
                raise RuntimeError("El contribuyente no tiene representados AGIP activos")
            buttons = [
                [InlineKeyboardButton(
                    f"{item['nombre']} — {visible_cuit(item['cuit'])}",
                    callback_data=f"ad:r:{state.nonce}:{item['id']}",
                )]
                for item in represented
            ]
            await self._send(
                adapter, chat_id,
                "Elegí el representado AGIP:",
                InlineKeyboardMarkup(buttons), thread_id,
            )
            return
        state.represented_id = int(row['id']); state.stage = "period"
        await self._send(adapter, chat_id, "Ingresá el período o fecha: AAAA, AAAAMM, AAAA-MM o DD/MM/AAAA.", thread_id=thread_id)

    async def _run_query(self, adapter, chat_id, thread_id, key: str, state: FlowState, period: str) -> None:
        execution_key: str | None = None
        execution_lock_acquired = False
        proc: asyncio.subprocess.Process | None = None
        try:
            if state.cancelled:
                return
            if state.contributor_id is None or state.represented_id is None:
                raise RuntimeError("AGIP_STATE_INVALID")
            execution_key = f"{state.contributor_id}:{state.represented_id}:{period}"
            owner = self.execution_locks.get(execution_key)
            if owner is not None and owner != key:
                raise RuntimeError("AGIP_EXECUTION_ALREADY_RUNNING")
            self._acquire_execution_lock(execution_key)
            execution_lock_acquired = True
            self.execution_locks[execution_key] = key
            if state.cancelled:
                return

            # The worker receives only opaque internal IDs and reads credentials locally.
            proc = await asyncio.create_subprocess_exec(
                "xvfb-run", "-a", "-s", "-screen 0 1440x1100x24 -nolisten tcp",
                "/home/pancho/hermes-workspace/agip-consulta-2025/.venv-selenium/bin/python",
                "/home/pancho/hermes-workspace/Contabot/scripts/agip-ddjj-worker.py",
                str(state.contributor_id), str(state.represented_id), period,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes[key] = proc
            if state.cancelled:
                await self._terminate_process(proc)
                return
            try:
                raw, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=_RUN_TIMEOUT_SECONDS
                )
            except TimeoutError:
                await self._terminate_process(proc)
                await self._finish(
                    adapter, chat_id, thread_id, state,
                    "Consulta AGIP cancelada porque superó el tiempo máximo. No se entregó ningún archivo.",
                )
                return
            if state.cancelled:
                return
            try:
                result = next(
                    json.loads(line)
                    for line in reversed(raw.decode(errors="replace").splitlines())
                    if line.strip().startswith("{")
                )
            except Exception:
                result = {"ok": False, "error_code": "AGIP_WORKER_RESULT_INVALID"}
            if not result.get("ok"):
                evidence = None
                try:
                    evidence = self._persist_failure(result, period, proc.returncode)
                except Exception as exc:
                    logger.error("[AGIP-DDJJ] failure evidence error_type=%s", type(exc).__name__)
                logger.warning(
                    "[AGIP-DDJJ] worker blocked code=%s evidence=%s",
                    result.get("error_code", "AGIP_WORKER_RESULT_INVALID"),
                    str(evidence) if evidence is not None else "unavailable",
                )
                await self._finish(
                    adapter, chat_id, thread_id, state,
                    self._worker_error_message(result, evidence is not None),
                )
                return
            xlsx = result.get("xlsx")
            path = os.path.realpath(str(xlsx or ""))
            if not (is_valid_delivery_path(path) and os.path.isfile(path)):
                await self._finish(
                    adapter, chat_id, thread_id, state,
                    "Consulta AGIP no completada: el Excel no pasó la validación de entrega.",
                )
                return
            delivery = await adapter.send_document(
                chat_id=str(chat_id), file_path=path, file_name=os.path.basename(path),
                caption="DDJJ IIBB — Excel con importes.",
                metadata={"thread_id": thread_id} if thread_id is not None else None,
            )
            if not delivery.success:
                logger.error("[AGIP-DDJJ] XLSX delivery failed: %s", type(delivery.error).__name__)
                await self._finish(
                    adapter, chat_id, thread_id, state,
                    "Consulta AGIP no completada: Telegram no confirmó la entrega del Excel.",
                )
                return
            logger.info("[AGIP-DDJJ] XLSX delivered message_id=%s", delivery.message_id)
            await self._finish(
                adapter, chat_id, thread_id, state,
                f"{result.get('message')} Excel enviado (mensaje {delivery.message_id}).",
            )
        except asyncio.CancelledError:
            state.cancelled = True
            if proc is not None and proc.returncode is None:
                await self._terminate_process(proc)
            raise
        except Exception as exc:
            logger.error("[AGIP-DDJJ] run failed error_type=%s", type(exc).__name__)
            if not state.cancelled:
                message = (
                    "Ya hay una consulta AGIP en curso para ese contribuyente y período."
                    if str(exc) == "AGIP_EXECUTION_ALREADY_RUNNING"
                    else "Consulta AGIP no completada por un error técnico."
                )
                await self._finish(adapter, chat_id, thread_id, state, message)
        finally:
            self.processes.pop(key, None)
            if execution_key and self.execution_locks.get(execution_key) == key:
                self.execution_locks.pop(execution_key, None)
            if execution_lock_acquired and execution_key:
                self._release_execution_lock(execution_key)

    def _release(self, key: str, task: asyncio.Task) -> None:
        self.tasks.pop(key, None)
        self.states.pop(key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[AGIP-DDJJ] background task failed")
