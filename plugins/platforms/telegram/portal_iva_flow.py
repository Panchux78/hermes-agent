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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

try:
    import fcntl
except ImportError:  # Windows gateway: keep Telegram importable, hide this Linux-only feature.
    fcntl = None

logger = logging.getLogger(__name__)
_CLIENTES_ROOT = Path("/home/pancho/clientes")
_EXECUTOR = Path("/home/pancho/procedimientos/portal-iva/portal_iva.py")
_UV = Path("/home/pancho/.hermes/bin/uv")
_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_LOCK_ROOT = Path("/home/pancho/.local/state/contabot/portal-iva/telegram-locks")
_RUN_TIMEOUT_SECONDS = 1800


@dataclass
class FlowState:
    user_id: str
    nonce: str
    stage: str
    contributor_id: int | None = None
    slug: str | None = None
    cuit: str | None = None
    nombre: str | None = None
    period: str | None = None
    progress_message: Any = None
    cancelled: bool = False
    created_at: float = field(default_factory=time.monotonic)


class PortalIvaFlow:
    """Privileged Telegram flow; credentials are read only by portal_iva.py."""

    def __init__(self, *, executor: Path = _EXECUTOR, uv: Path = _UV, clients_root: Path = _CLIENTES_ROOT) -> None:
        self.executor = Path(executor)
        self.uv = Path(uv)
        self.clients_root = Path(clients_root)
        self.states: dict[str, FlowState] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.execution_locks: dict[str, str] = {}
        self.execution_lock_fds: dict[str, int] = {}

    def available(self) -> bool:
        return fcntl is not None and self.executor.is_file() and self.uv.is_file() and self.clients_root.is_dir()

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _visible_cuit(cuit: str) -> str:
        return f"{cuit[:2]}-******-{cuit[-1:]}" if re.fullmatch(r"\d{11}", cuit or "") else "CUIT oculto"

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

    def _query(self, sql: str) -> list[dict[str, Any]]:
        run = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "--dbname=contabot", "-At", "-c", sql],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
        if run.returncode:
            raise RuntimeError("PORTAL_IVA_DATABASE_UNAVAILABLE")
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def _search(self, term: str) -> list[dict[str, Any]]:
        encoded = self._sql_scalar(term.strip())
        value = f"convert_from(decode('{encoded}','base64'),'UTF8')"
        match = (
            f"(lower(c.nombre_legal) LIKE '%'||lower({value})||'%' "
            f"OR c.slug=lower({value}) OR c.cuit=regexp_replace({value},'[^0-9]','','g'))"
        )
        sql = f"""
            SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,
                'cuit',btrim(c.cuit),'slug',c.slug)::text
            FROM tbl_contribuyentes c
            WHERE c.activo AND {match}
              AND 1 = (
                SELECT count(*) FROM tbl_representaciones r
                JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='ARCA' AND e.activo
                JOIN tbl_accesos a ON a.id_entidad=r.id_entidad
                    AND a.id_contribuyente=r.id_contribuyente_representante AND a.activo
                JOIN tbl_contribuyentes holder ON holder.id_contribuyente=a.id_contribuyente AND holder.activo
                WHERE r.activo AND r.id_contribuyente_representado=c.id_contribuyente
              )
            ORDER BY c.nombre_legal LIMIT 12;
        """
        return self._query(sql)

    def _by_id(self, item_id: int) -> list[dict[str, Any]]:
        return self._query(f"""
            SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,
                'cuit',btrim(c.cuit),'slug',c.slug)::text
            FROM tbl_contribuyentes c
            WHERE c.id_contribuyente={int(item_id)} AND c.activo
              AND 1 = (
                SELECT count(*) FROM tbl_representaciones r
                JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='ARCA' AND e.activo
                JOIN tbl_accesos a ON a.id_entidad=r.id_entidad
                    AND a.id_contribuyente=r.id_contribuyente_representante AND a.activo
                JOIN tbl_contribuyentes holder ON holder.id_contribuyente=a.id_contribuyente AND holder.activo
                WHERE r.activo AND r.id_contribuyente_representado=c.id_contribuyente
              );
        """)

    @staticmethod
    def _cancel_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"pi:cancel:{nonce}")]])

    async def start(self, adapter, query, chat_id: Any, thread_id: Any, user_id: str) -> None:
        key = self._key(chat_id, thread_id, user_id)
        if key in self.tasks:
            await query.answer("Ya hay una descarga Portal IVA en curso.")
            return
        self.states[key] = FlowState(
            user_id=user_id, nonce=uuid.uuid4().hex[:10], stage="client", created_at=time.monotonic(),
        )
        await query.answer("Portal IVA → CSV")
        await self._send(
            adapter, chat_id, "Ingresá nombre, CUIT o slug del contribuyente.", thread_id,
            self._cancel_keyboard(self.states[key].nonce),
        )

    async def callback(self, adapter, query, data: str, chat_id: Any, thread_id: Any, user_id: str) -> bool:
        key = self._key(chat_id, thread_id, user_id)
        if data == "pi:start":
            await self.start(adapter, query, chat_id, thread_id, user_id)
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
            await query.answer("Esta solicitud venció. Iniciá Portal IVA nuevamente.")
            return True
        if not state or state.nonce != select.group(1) or state.stage != "client":
            await query.answer("Esta selección venció. Iniciá Portal IVA nuevamente.")
            return True
        try:
            found = await asyncio.to_thread(self._by_id, int(select.group(2)))
        except Exception:
            await query.answer("No pude validar la opción en la base. Probá nuevamente.")
            return True
        if not found:
            await query.answer("La opción ya no está disponible.")
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
        if state.stage != "running" and time.monotonic() - state.created_at > 600:
            self.states.pop(key, None)
            await self._send(adapter, chat_id, "La solicitud venció. Iniciá Portal IVA nuevamente.", thread_id)
            return True
        if state.stage in {"running", "delivering"}:
            await self._send(adapter, chat_id, "Portal IVA ya está en ejecución para esta solicitud.", thread_id)
            return True
        if state.stage == "period":
            period = (message.text or "").strip()
            if not _PERIOD.fullmatch(period):
                await self._send(adapter, chat_id, "Ingresá el período exactamente como AAAA-MM.", thread_id)
                return True
            if key in self.tasks:
                await self._send(adapter, chat_id, "Ya hay una descarga Portal IVA en curso.", thread_id)
                return True
            state.period = period
            state.stage = "running"
            state.progress_message = await self._send(
                adapter, chat_id, "Preparando Portal IVA…", thread_id, self._cancel_keyboard(state.nonce),
            )
            task = asyncio.create_task(self._run(adapter, chat_id, thread_id, key, state))
            self.tasks[key] = task
            task.add_done_callback(lambda done: self._release(key, done))
            return True
        try:
            candidates = await asyncio.to_thread(self._search, message.text or "")
        except Exception:
            await self._send(adapter, chat_id, "No pude consultar la base canónica. Probá nuevamente.", thread_id)
            return True
        if not candidates:
            await self._send(adapter, chat_id, "No hay un contribuyente ARCA activo con acceso y representación válidos que coincida. Probá con nombre, CUIT o slug.", thread_id)
            return True
        if len(candidates) == 1:
            await self._select(adapter, chat_id, thread_id, state, candidates[0])
            return True
        rows = [[InlineKeyboardButton(f"{item['nombre']} — {self._visible_cuit(item['cuit'])}", callback_data=f"pi:select:{state.nonce}:{item['id']}")] for item in candidates]
        await self._send(adapter, chat_id, "Elegí un contribuyente:", thread_id, InlineKeyboardMarkup(rows))
        return True

    async def _select(self, adapter, chat_id: Any, thread_id: Any, state: FlowState, item: dict[str, Any]) -> None:
        state.contributor_id = int(item["id"])
        state.slug = str(item["slug"])
        state.cuit = str(item["cuit"])
        state.nombre = str(item["nombre"])
        state.stage = "period"
        await self._send(adapter, chat_id, "Ingresá el período exactamente como AAAA-MM.", thread_id)

    def _command(self, slug: str, period: str) -> list[str]:
        if not self.available():
            raise RuntimeError("PORTAL_IVA_RUNTIME_UNAVAILABLE")
        return [
            str(self.uv), "run", "--with", "selenium", "xvfb-run", "-a",
            "python3", str(self.executor), "--cliente", slug, "--periodo", period,
        ]

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
            await self._edit_progress(state, f"Preparando Portal IVA… {elapsed} s", keyboard=self._cancel_keyboard(state.nonce))

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
    def _error_message(result: dict[str, Any] | None, fallback: str) -> str:
        reason = str((result or {}).get("motivo", ""))
        if reason.startswith("CREDENCIAL_") or "CREDENTIAL" in reason:
            return "Portal IVA no se completó: ARCA rechazó la credencial."
        if reason.startswith("PERIODO_NO_DISPONIBLE"):
            return "Portal IVA no se completó: el período no está disponible."
        if reason:
            return "Portal IVA no se completó. La evidencia quedó preservada para revisión."
        return fallback

    async def _run(self, adapter, chat_id: Any, thread_id: Any, key: str, state: FlowState) -> None:
        ticker = None
        result: dict[str, Any] | None = None
        staging: Path | None = None
        proc: asyncio.subprocess.Process | None = None
        execution_key: str | None = None
        execution_lock_acquired = False
        try:
            if state.cancelled:
                return
            if state.contributor_id is None or not state.slug or not state.cuit or not state.period:
                raise RuntimeError("PORTAL_IVA_STATE_INVALID")
            contributor_id = state.contributor_id
            slug, cuit, period = state.slug, state.cuit, state.period
            verified = await asyncio.to_thread(self._by_id, state.contributor_id)
            if state.cancelled:
                return
            if len(verified) != 1 or verified[0].get("slug") != slug or verified[0].get("cuit") != cuit:
                raise RuntimeError("PORTAL_IVA_SELECTION_STALE")
            execution_key = f"{contributor_id}:{period}"
            owner = self.execution_locks.get(execution_key)
            if owner is not None and owner != key:
                raise RuntimeError("PORTAL_IVA_EXECUTION_ALREADY_RUNNING")
            self._acquire_execution_lock(execution_key)
            execution_lock_acquired = True
            self.execution_locks[execution_key] = key
            command = self._command(slug, period)
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            )
            self.processes[key] = proc
            if state.cancelled:
                await self._terminate_process(proc)
                return
            ticker = asyncio.create_task(self._ticker(state, started))
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=_RUN_TIMEOUT_SECONDS)
            except TimeoutError:
                await self._terminate_process(proc)
                raise RuntimeError("PORTAL_IVA_TIMEOUT")
            if state.cancelled:
                return
            result = self._parse_result(raw)
            if proc.returncode != 0 or not result.get("ok"):
                await self._edit_progress(state, self._error_message(result, "Portal IVA no se completó. La evidencia quedó preservada para revisión."))
                return
            if result.get("etapa") != "completado":
                raise RuntimeError("PORTAL_IVA_OUTPUT_INCOMPLETE")
            deliverables = self._deliverables(slug, cuit, period, result)
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
            warnings = result.get("advertencias") if isinstance(result.get("advertencias"), list) else []
            suffix = f" Advertencias: {', '.join(str(x) for x in warnings)}." if warnings else ""
            await self._edit_progress(state, f"Portal IVA completado. Ventas y Compras enviadas.{suffix}")
        except asyncio.CancelledError:
            state.cancelled = True
            if proc is not None and proc.returncode is None:
                await self._terminate_process(proc)
            raise
        except Exception as exc:
            logger.error("[PORTAL-IVA] run failed error_type=%s", type(exc).__name__)
            if not state.cancelled:
                await self._edit_progress(state, self._error_message(result, "Portal IVA no se completó. La evidencia quedó preservada para revisión."))
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

    def _release(self, key: str, task: asyncio.Task) -> None:
        self.tasks.pop(key, None)
        self.states.pop(key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[PORTAL-IVA] background task failed")
