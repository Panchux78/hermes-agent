"""Administrative Telegram maintenance flows for ContaBot."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_CONTABOT = Path("/home/pancho/hermes-workspace/Contabot")
_ARCA_REBUILD = _CONTABOT / "skills/accounting/arca-catalogos/scripts/reconstruir_mapa_impuestos_arca.py"
_ARCA_COMPARE = _CONTABOT / "skills/accounting/arca-catalogos/scripts/comparar_mapa_impuestos_arca.py"
_BCRA_UPDATE = _CONTABOT / "database/scripts/actualizar_bancos_bcra.py"
_ARCA_CURRENT = Path("/home/pancho/hermes-workspace/vectux.com/root/mapa_impuestos.json")
_STATE_ROOT = Path("/home/pancho/.local/state/contabot/admin")


@dataclass
class CandidateState:
    kind: str
    nonce: str
    candidate: Path
    report: Path
    candidate_sha256: str
    current_sha256: str | None
    created_at: datetime


class AdminMaintenanceFlow:
    """Private, technical-user-only candidate/confirm workflows."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._operations: dict[str, tuple[str, datetime]] = {}
        self._progress_messages: dict[str, Any] = {}
        self._candidates: dict[str, CandidateState] = {}

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    def _release_task(self, key: str, task: asyncio.Task) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self._operations.pop(key, None)
            self._progress_messages.pop(key, None)

    @staticmethod
    async def _send(adapter, chat_id: Any, text: str, keyboard=None, thread_id=None) -> Any:
        kwargs = {"chat_id": chat_id, "text": text, "reply_markup": keyboard}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        return await adapter._bot.send_message(**kwargs)

    @staticmethod
    async def _progress(adapter, chat_id: Any, message_id: Any, text: str, keyboard=None, thread_id=None) -> Any:
        if message_id is None:
            return await AdminMaintenanceFlow._send(adapter, chat_id, text, keyboard, thread_id)
        return await adapter._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _cleanup_state(state: CandidateState) -> None:
        run_dir = state.candidate.parent
        if run_dir.is_dir() and not run_dir.is_symlink():
            shutil.rmtree(run_dir)

    @staticmethod
    def _private_run_dir() -> Path:
        run_dir = _STATE_ROOT / uuid.uuid4().hex
        run_dir.mkdir(mode=0o700, parents=True)
        os.chmod(run_dir, 0o700)
        return run_dir

    @staticmethod
    async def _run(*command: str, progress=None) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        async def consume_stderr():
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                match = __import__('re').fullmatch(r"progress=(\d+)/(\d+)", line.decode('utf-8','replace').strip())
                if match and progress is not None:
                    await progress(int(match.group(1)), int(match.group(2)))
        stderr_task = asyncio.create_task(consume_stderr())
        try:
            stdout = await process.stdout.read()
            await process.wait()
            await stderr_task
        except asyncio.CancelledError:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            stderr_task.cancel()
            raise
        if process.returncode != 0:
            raise RuntimeError("administrative_candidate_failed")
        return json.loads(stdout.decode("utf-8"))

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id) -> bool:
        if not data.startswith("oa:"):
            return False
        key = self._key(chat_id, thread_id, user_id)
        now = datetime.now(timezone.utc)
        for stale_key, stale_state in list(self._candidates.items()):
            if now - stale_state.created_at > timedelta(minutes=15):
                self._candidates.pop(stale_key, None)
                self._cleanup_state(stale_state)
        if data == "oa:cancelrun":
            task = self._tasks.get(key)
            if task and not task.done():
                progress_message_id = self._progress_messages.get(key)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await query.answer("Cancelando actualización…")
                await self._progress(adapter, chat_id, progress_message_id, "Actualización cancelada. No se modificó el estado vigente.", thread_id=thread_id)
            else:
                await query.answer("No hay una actualización activa.")
            return True
        if data in {"oa:arca:start", "oa:bcra:start"}:
            if key in self._candidates:
                await query.answer("Hay un candidato pendiente. Confirmalo, cancelalo o esperá su vencimiento.")
                return True
            if key in self._tasks and not self._tasks[key].done():
                kind, started = self._operations[key]
                minutes = max(0, int((datetime.now(timezone.utc) - started).total_seconds() // 60))
                await query.answer(f"Está en curso {kind} desde hace {minutes} minutos. La nueva operación no se inició ni quedó en espera. Podés cancelarla desde el mensaje de progreso.")
                return True
            kind = "arca" if data == "oa:arca:start" else "bcra"
            await query.answer("Preparando candidato…")
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            text = "Actualizando mapa de impuestos. Puede tardar varios minutos. Te voy informando el progreso." if kind == "arca" else "Consultando el padrón oficial BCRA. Puede tardar unos minutos. Te voy informando el progreso."
            progress = await self._send(adapter, chat_id, text, InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar actualización", callback_data="oa:cancelrun")]]), thread_id)
            task = asyncio.create_task(self._prepare(adapter, chat_id, thread_id, user_id, kind, getattr(progress, "message_id", None)))
            self._tasks[key] = task
            self._operations[key] = ("actualización del mapa ARCA" if kind == "arca" else "consulta del padrón BCRA", datetime.now(timezone.utc))
            self._progress_messages[key] = getattr(progress, "message_id", None)
            task.add_done_callback(lambda done: self._release_task(key, done))
            return True
        parts = data.split(":")
        if len(parts) != 4 or parts[1] not in {"arca", "bcra"} or parts[2] not in {"confirm", "cancel"}:
            await query.answer("Acción administrativa inválida.")
            return True
        state = self._candidates.get(key)
        if state is None or state.kind != parts[1] or state.nonce != parts[3]:
            await query.answer("Este candidato venció. Generá uno nuevo.")
            return True
        if parts[2] == "cancel":
            self._candidates.pop(key, None)
            self._cleanup_state(state)
            await query.answer("Actualización cancelada.")
            return True
        self._candidates.pop(key, None)
        await query.answer("Confirmación recibida.")
        try:
            if state.kind == "arca":
                await self._promote_arca(adapter, chat_id, thread_id, state)
            else:
                await self._apply_bcra(adapter, chat_id, thread_id, state)
        except Exception:
            await self._send(adapter, chat_id, "No se pudo confirmar la actualización. El estado vigente no cambió.", thread_id=thread_id)
        finally:
            self._cleanup_state(state)
        return True

    async def _prepare(self, adapter, chat_id, thread_id, user_id, kind: str, progress_message_id=None) -> None:
        try:
            if kind == "arca":
                await self._prepare_arca(adapter, chat_id, thread_id, user_id, progress_message_id)
            else:
                await self._prepare_bcra(adapter, chat_id, thread_id, user_id, progress_message_id)
        except Exception:
            await self._progress(adapter, chat_id, progress_message_id, "No se pudo preparar la actualización. El estado vigente no cambió.", thread_id=thread_id)

    async def _prepare_arca(self, adapter, chat_id, thread_id, user_id, progress_message_id=None) -> None:
        run_dir = self._private_run_dir()
        try:
            candidate = run_dir / "mapa-candidato.json"
            report = run_dir / "comparacion.json"
            async def progress_cb(done, total):
                await self._progress(adapter, chat_id, progress_message_id, f"ARCA: {done} de {total} impuestos procesados.", thread_id=thread_id)
            await self._run(sys.executable, str(_ARCA_REBUILD), str(candidate), progress=progress_cb)
            comparison = await self._run(sys.executable, str(_ARCA_COMPARE), str(_ARCA_CURRENT), str(candidate), str(report))
            diffs = comparison["diffs"]
            changed = any(diffs[part][field] for part in diffs for field in diffs[part])
            if not changed:
                shutil.rmtree(run_dir)
                await self._progress(adapter, chat_id, progress_message_id, "El mapa de impuestos no tiene cambios.", thread_id=thread_id)
                return
            nonce = uuid.uuid4().hex[:16]
            self._candidates[self._key(chat_id, thread_id, user_id)] = CandidateState(
                "arca", nonce, candidate, report, self._sha256(candidate), self._sha256(_ARCA_CURRENT), datetime.now(timezone.utc)
            )
            summary = {
                "altas": sum(diffs[part]["added"] for part in diffs),
                "bajas": sum(diffs[part]["removed"] for part in diffs),
                "modificaciones": sum(diffs[part]["modified"] for part in diffs),
            }
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirmar actualización", callback_data=f"oa:arca:confirm:{nonce}"),
                InlineKeyboardButton("Cancelar", callback_data=f"oa:arca:cancel:{nonce}"),
            ]])
            await self._progress(adapter, chat_id, progress_message_id, f"Mapa de impuestos: altas {summary['altas']}, bajas {summary['bajas']}, modificaciones {summary['modificaciones']}.", keyboard, thread_id)
        except Exception:
            if run_dir.exists() and not run_dir.is_symlink():
                shutil.rmtree(run_dir)
            raise

    async def _prepare_bcra(self, adapter, chat_id, thread_id, user_id, progress_message_id=None) -> None:
        run_dir = self._private_run_dir()
        try:
            candidate = run_dir / "bancos-candidato.json"
            current = run_dir / "bancos-vigentes.json"
            report = run_dir / "comparacion.json"
            async def progress_cb(done, total):
                await self._progress(adapter, chat_id, progress_message_id, f"BCRA: {done} de {total} entidades consultadas.", thread_id=thread_id)
            result = await self._run(sys.executable, str(_BCRA_UPDATE), "candidate", "--output", str(candidate), progress=progress_cb)
            if result.get("count", 0) <= 0:
                raise RuntimeError("bcra_candidate_empty")
            config = self._catalog_args()
            await self._run(sys.executable, str(_BCRA_UPDATE), "snapshot", "--output", str(current), "--period", result["periodo_bcra"], *config)
            comparison = await self._run(sys.executable, str(_BCRA_UPDATE), "compare", "--current", str(current), "--candidate", str(candidate), "--output", str(report))
            if not any(comparison[key] for key in ("nuevos", "vinculaciones", "ausentes", "reapariciones", "modificaciones")):
                shutil.rmtree(run_dir)
                await self._progress(adapter, chat_id, progress_message_id, "El padrón BCRA no tiene cambios.", thread_id=thread_id)
                return
            nonce = uuid.uuid4().hex[:16]
            self._candidates[self._key(chat_id, thread_id, user_id)] = CandidateState(
                "bcra", nonce, candidate, report, self._sha256(candidate), self._sha256(current), datetime.now(timezone.utc)
            )
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Confirmar actualización", callback_data=f"oa:bcra:confirm:{nonce}"),
                InlineKeyboardButton("Cancelar", callback_data=f"oa:bcra:cancel:{nonce}"),
            ]])
            summary = f"{comparison['nuevos']} bancos nuevos, {comparison['vinculaciones']} vinculaciones existentes, {comparison['reapariciones']} reapariciones, {comparison['ausentes']} ausentes, {comparison['modificaciones']} modificaciones"
            await self._progress(adapter, chat_id, progress_message_id, f"Candidato BCRA preparado: {summary}.", keyboard, thread_id)
        except Exception:
            if run_dir.exists() and not run_dir.is_symlink():
                shutil.rmtree(run_dir)
            raise

    @staticmethod
    def _catalog_args() -> tuple[str, ...]:
        fields = {
            "--host": os.getenv("CONTABOT_ROUTER_CATALOG_HOST", ""),
            "--port": os.getenv("CONTABOT_ROUTER_CATALOG_PORT", ""),
            "--database": os.getenv("CONTABOT_ROUTER_CATALOG_DATABASE", ""),
            "--user": os.getenv("CONTABOT_ROUTER_CATALOG_USER", ""),
            "--pgpass": os.getenv("CONTABOT_ROUTER_CATALOG_PGPASSFILE", ""),
        }
        if not all(fields.values()):
            raise RuntimeError("bcra_runtime_catalog_configuration_missing")
        return tuple(item for pair in fields.items() for item in pair)

    async def _promote_arca(self, adapter, chat_id, thread_id, state: CandidateState) -> None:
        if self._sha256(state.candidate) != state.candidate_sha256 or self._sha256(_ARCA_CURRENT) != state.current_sha256:
            raise RuntimeError("arca_candidate_or_current_changed")
        backup = _ARCA_CURRENT.with_name(f"{_ARCA_CURRENT.name}.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        temporary = _ARCA_CURRENT.with_name(f".{_ARCA_CURRENT.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(_ARCA_CURRENT, backup)
            os.chmod(backup, 0o600)
            shutil.copyfile(state.candidate, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, _ARCA_CURRENT)
            os.chmod(_ARCA_CURRENT, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        await self._send(adapter, chat_id, "Mapa de impuestos actualizado.", thread_id=thread_id)

    async def _snapshot_bcra(self, state: CandidateState) -> str:
        current = state.candidate.parent / "bancos-confirmacion.json"
        period = json.loads(state.candidate.read_text(encoding="utf-8"))["periodo_bcra"]
        await self._run(sys.executable, str(_BCRA_UPDATE), "snapshot", "--output", str(current), "--period", period, *self._catalog_args())
        return self._sha256(current)

    async def _apply_bcra(self, adapter, chat_id, thread_id, state: CandidateState) -> None:
        if self._sha256(state.candidate) != state.candidate_sha256:
            await self._send(adapter, chat_id, "El candidato cambió. Generá uno nuevo.", thread_id=thread_id)
            return
        if await self._snapshot_bcra(state) != state.current_sha256:
            await self._send(adapter, chat_id, "El padrón BCRA cambió. Generá un candidato nuevo.", thread_id=thread_id)
            return
        fields = {
            "--host": os.getenv("CONTABOT_BCRA_WRITE_HOST", ""),
            "--port": os.getenv("CONTABOT_BCRA_WRITE_PORT", ""),
            "--database": os.getenv("CONTABOT_BCRA_WRITE_DATABASE", ""),
            "--user": os.getenv("CONTABOT_BCRA_WRITE_USER", ""),
            "--pgpass": os.getenv("CONTABOT_BCRA_WRITE_PGPASSFILE", ""),
        }
        if not all(fields.values()):
            await self._send(adapter, chat_id, "La identidad de escritura BCRA no está configurada.", thread_id=thread_id)
            return
        await self._run(sys.executable, str(_BCRA_UPDATE), "apply", "--candidate", str(state.candidate), *(item for pair in fields.items() for item in pair))
        await self._send(adapter, chat_id, "Padrón BCRA actualizado.", thread_id=thread_id)
