"""Flujo Telegram para lotes ZIP de resúmenes bancarios."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow

logger = logging.getLogger(__name__)
_PROJECT = Path("/home/pancho/hermes-workspace/Contabot")
_BATCH_ROOT = Path("/home/pancho/clientes/_recepcion_lotes")
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class BatchUploadRequest:
    user_id: str


class BatchPdfXlsxFlow:
    def __init__(self, project_dir: Path = _PROJECT) -> None:
        self.project_dir = Path(os.getenv("CONTA_PDF_ROUTER_PROJECT_DIR", str(project_dir)))
        self.batch_root = Path(os.getenv("CONTABOT_BATCH_ROOT", str(_BATCH_ROOT)))
        self.input_cache = Path(os.getenv("CONTABOT_BATCH_INPUT_CACHE_DIR", "/home/pancho/.hermes/cache/batch-pdf-xlsx-inputs"))
        self.requests: dict[str, BatchUploadRequest] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    async def _send(adapter, chat_id: Any, text: str, thread_id: Any = None, reply_markup: Any = None):
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await adapter._bot.send_message(**kwargs)

    def _command(self, action: str, *arguments: str) -> list[str]:
        uv = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "bin" / "uv"
        if not uv.is_file():
            raise RuntimeError("batch_router_runtime_unavailable")
        return [str(uv), "run", "--with", "pdfplumber", "--with", "openpyxl", "python3", "skills/accounting/pdf-contable-router/scripts/router.py", action, *arguments]

    async def _run(self, key: str, command: list[str], progress: Callable[[str], Awaitable[None]] | None = None) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(*command, cwd=str(self.project_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.processes[key] = proc
        stderr_chunks: list[bytes] = []

        async def read_stderr() -> None:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_chunks.append(line)
                text = line.decode("utf-8", errors="replace").strip()
                if progress is not None and text.startswith("progress="):
                    await progress(text.removeprefix("progress="))

        stderr_task = asyncio.create_task(read_stderr())
        try:
            raw = await proc.stdout.read()
            await proc.wait()
            await stderr_task
        finally:
            self.processes.pop(key, None)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.error("[BATCH-XLSX] invalid router response stderr_present=%s", bool(stderr_chunks))
            raise RuntimeError("batch_router_response_invalid") from exc
        if not isinstance(result, dict):
            raise RuntimeError("batch_router_response_invalid")
        return result

    async def callback(self, adapter, query, data: str, chat_id: Any, thread_id: Any, user_id: str) -> bool:
        key = self._key(chat_id, thread_id, user_id)
        if data == "bx:start":
            self.requests[key] = BatchUploadRequest(user_id=user_id)
            await query.answer("Lote de resúmenes bancarios → Excel")
            await self._send(adapter, chat_id, "Mandame un ZIP con los resúmenes bancarios. Primero lo voy a revisar; no se convierte nada hasta que confirmes.", thread_id)
            return True
        if data.startswith("bx:p:"):
            batch_id = data[5:]
            await query.answer("Procesando lote")
            await query.edit_message_text("Procesando el lote…")
            status = await self._status(key, batch_id, user_id, str(chat_id))
            digest = status.get("digest")
            if not isinstance(digest, str):
                await query.edit_message_text("No pude recuperar la confirmación del lote. El archivo original sigue conservado.")
                return True

            async def progress(event: str) -> None:
                try:
                    phase, counts = event.split(":", 1)
                    current, total = counts.split("/", 1)
                    label = "Procesando PDF" if phase == "processing" else "Preparando XLSX"
                    await query.edit_message_text(f"{label} {current} de {total}…")
                except Exception:
                    return

            if status.get("state") == "COMPLETED":
                result = {"status": "COMPLETED", "outputs": status.get("outputs", [])}
            else:
                result = await self._run(key, self._command("batch-process", "--batch-id", batch_id, "--confirmation-digest", digest, "--batch-root", str(self.batch_root), "--telegram-user-id", user_id, "--telegram-chat-id", str(chat_id)), progress)
                if result.get("status") != "COMPLETED":
                    await query.edit_message_text("No pude completar el lote. El ZIP original y el avance ya realizado quedaron conservados para revisión.")
                    return True
            outputs = result.get("outputs") if isinstance(result.get("outputs"), list) else []
            await query.edit_message_text(f"Lote procesado. Entregando {len(outputs)} archivo(s)…")
            delivered = 0
            for output in outputs:
                path = Path(str(output.get("path", "")))
                if output.get("delivered") is True:
                    delivered += 1
                    continue
                delivery = await adapter.send_document(chat_id=str(chat_id), file_path=str(path), file_name=PdfXlsxFlow._delivery_filename(path), caption=f"{output.get('document_count', 0)} resumen(es), {output.get('row_count', 0)} movimiento(s).", metadata={"thread_id": thread_id} if thread_id is not None else None)
                if not delivery.success:
                    await query.edit_message_text("El lote se procesó, pero no pude entregar todos los Excel. Podés volver a tocar Procesar para reintentar los pendientes.")
                    return True
                await self._run(key, self._command("batch-delivered", "--batch-id", batch_id, "--output-sha256", str(output["sha256"]), "--batch-root", str(self.batch_root), "--telegram-user-id", user_id, "--telegram-chat-id", str(chat_id)))
                delivered += 1
            await query.edit_message_text(f"Listo. Se entregaron {delivered} archivo(s) Excel.")
            return True
        if data.startswith("bx:c:"):
            batch_id = data[5:]
            await query.answer("Cancelando")
            proc = self.processes.get(key)
            if proc is not None and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            result = await self._run(key, self._command("batch-cancel", "--batch-id", batch_id, "--batch-root", str(self.batch_root), "--telegram-user-id", user_id, "--telegram-chat-id", str(chat_id)))
            await query.edit_message_text("Lote cancelado. El archivo original quedó conservado." if result.get("status") == "CANCELLED" else "No pude cancelar el lote en su estado actual.")
            return True
        return False

    async def _status(self, key: str, batch_id: str, user_id: str, chat_id: str) -> dict[str, Any]:
        return await self._run(key, self._command("batch-status", "--batch-id", batch_id, "--batch-root", str(self.batch_root), "--telegram-user-id", user_id, "--telegram-chat-id", chat_id))

    async def document(self, adapter, message) -> bool:
        chat_id = message.chat_id
        thread_id = getattr(message, "message_thread_id", None)
        user_id = str(getattr(message.from_user, "id", ""))
        key = self._key(chat_id, thread_id, user_id)
        if key not in self.requests:
            return False
        document = getattr(message, "document", None)
        name = Path(str(getattr(document, "file_name", "") or "")).name
        size = int(getattr(document, "file_size", 0) or 0)
        if not document or Path(name).suffix.lower() not in {".zip", ".rar"}:
            await self._send(adapter, chat_id, "Esperaba un archivo ZIP o RAR con PDFs.", thread_id)
            return True
        if size <= 0 or size > _MAX_ARCHIVE_BYTES:
            await self._send(adapter, chat_id, "El archivo supera el límite de 20 MB o Telegram no informó su tamaño.", thread_id)
            return True
        self.requests.pop(key, None)
        progress_message = await self._send(adapter, chat_id, "Recibiendo y analizando el lote…", thread_id)
        staging = self.input_cache / uuid.uuid4().hex
        staging.mkdir(mode=0o700, parents=True)
        source = staging / name
        try:
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=source)
            source.chmod(0o600)
            result = await self._run(key, self._command("batch-inspect", "--input", str(source), "--batch-root", str(self.batch_root), "--telegram-user-id", user_id, "--telegram-chat-id", str(chat_id)))
        except Exception:
            logger.exception("[BATCH-XLSX] inspect failed")
            await progress_message.edit_text("Ocurrió un error técnico al revisar el lote. Si el archivo pudo preservarse, quedó disponible para revisión.")
            return True
        finally:
            if staging.is_dir() and staging.parent == self.input_cache:
                shutil.rmtree(staging)
        if result.get("status") != "AWAITING_CONFIRMATION":
            problems = result.get("problems") if isinstance(result.get("problems"), list) else []
            lines = ["No se procesó el lote."]
            for problem in problems[:20]:
                lines.append(f"- {problem.get('member_name', 'PDF')}: {problem.get('reason', 'no identificado')}")
            if not problems:
                lines.append(f"- {result.get('reason', 'No pude identificar todos los documentos.')}")
            lines.append("El archivo original quedó conservado.")
            await progress_message.edit_text("\n".join(lines))
            return True
        groups = result.get("groups") if isinstance(result.get("groups"), list) else []
        output_count = len(groups)
        output_text = "se generará 1 XLSX" if output_count == 1 else f"se generarán {output_count} XLSX"
        lines = [f"Detecté {result.get('pdf_count', 0)} PDFs y {output_text}.", ""]
        for group in groups:
            periods = group.get("periods") or []
            period_start = group.get("period_start") or (periods[0] if periods else "sin período")
            period_end = group.get("period_end") or (periods[-1] if periods else period_start)
            span = str(period_start) if period_start == period_end else f"{period_start} a {period_end}"
            pdf_count = int(group.get("pdf_count") or 0)
            pdf_label = "PDF" if pdf_count == 1 else "PDFs"
            lines.append(f"{group.get('contributor_name')} — {group.get('entity_name')}: {pdf_count} {pdf_label}, {', '.join(group.get('currencies') or [])}, {span}")
            if group.get("is_twelve_month_period"):
                lines.append("12 meses consecutivos")
            elif group.get("is_contiguous"):
                lines.append(f"{group.get('distinct_period_count', len(periods))} meses consecutivos")
            missing = group.get("missing_periods") or []
            if missing:
                lines.append(f"Advertencia: faltan los períodos {', '.join(str(value) for value in missing)}")
            repeated = group.get("repeated_periods") or []
            if repeated:
                lines.append(f"Advertencia: hay más de un PDF para {', '.join(str(value) for value in repeated)}")
        batch_id = str(result["batch_id"])
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Procesar", callback_data=f"bx:p:{batch_id}"), InlineKeyboardButton("Cancelar", callback_data=f"bx:c:{batch_id}")]])
        await progress_message.edit_text("\n".join(lines), reply_markup=keyboard)
        return True
