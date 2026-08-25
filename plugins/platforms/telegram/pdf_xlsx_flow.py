"""Flujo Telegram autorizado para convertir un PDF recibido a XLSX."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)
_DEFAULT_PROJECT_DIR = Path("/home/pancho/hermes-workspace/conversion-documentos-contables-xlsx")
_DEFAULT_ROUTER_PROJECT_DIR = Path("/home/pancho/hermes-workspace/Contabot")
_MAX_PDF_BYTES = 5_000_000


@dataclass(frozen=True)
class PdfXlsxRequest:
    user_id: str


class ConversionFailure(RuntimeError):
    """Falla saneada del subprocess de conversión."""

    def __init__(self, status: str, reason: str, run_id: str) -> None:
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason
        self.run_id = run_id


class PdfXlsxFlow:
    """One pending PDF→XLSX conversion per authorized Telegram sender."""

    def __init__(self, project_dir: Path = _DEFAULT_PROJECT_DIR) -> None:
        self.project_dir = Path(project_dir)
        router_project_dir = os.getenv("CONTA_PDF_ROUTER_PROJECT_DIR")
        self.router_project_dir = (
            Path(router_project_dir)
            if router_project_dir
            else _DEFAULT_ROUTER_PROJECT_DIR if self.project_dir == _DEFAULT_PROJECT_DIR else None
        )
        self.requests: dict[str, PdfXlsxRequest] = {}
        self.input_cache_dir = Path(os.getenv(
            "CONTA_PDF_XLSX_INPUT_CACHE_DIR",
            "/home/pancho/.hermes/cache/pdf-xlsx-inputs",
        ))
        self.document_timeout_seconds = self._positive_float(
            os.getenv("CONTA_PDF_XLSX_DOCUMENT_TIMEOUT_SECONDS"), 3600.0
        )

    @staticmethod
    def _positive_float(value: str | None, default: float) -> float:
        try:
            parsed = float(value) if value is not None else default
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    async def _send(adapter, chat_id, text: str, thread_id=None) -> None:
        kwargs = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await adapter._bot.send_message(**kwargs)

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id) -> bool:
        if data != "px:start":
            return False
        self.requests[self._key(chat_id, thread_id, user_id)] = PdfXlsxRequest(user_id=str(user_id))
        await query.answer("Convertir PDF a Excel")
        await self._send(adapter, chat_id, "Mandame el PDF que querés convertir a Excel.", thread_id)
        return True

    async def document(self, adapter, message) -> bool:
        chat_id = message.chat_id
        thread_id = getattr(message, "message_thread_id", None)
        user_id = str(getattr(message.from_user, "id", ""))
        key = self._key(chat_id, thread_id, user_id)
        if key not in self.requests:
            return False
        document = getattr(message, "document", None)
        name = str(getattr(document, "file_name", "") or "").lower()
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
        size = int(getattr(document, "file_size", 0) or 0)
        if not document or not (name.endswith(".pdf") or mime_type == "application/pdf"):
            await self._send(adapter, chat_id, "Esperaba un archivo PDF. Mandá el PDF para convertirlo a Excel.", thread_id)
            return True
        if size <= 0 or size > _MAX_PDF_BYTES:
            await self._send(adapter, chat_id, "El PDF supera el límite de 5 MB o Telegram no informó su tamaño.", thread_id)
            return True

        self.requests.pop(key, None)
        reported_stages: set[str] = set()

        async def report_progress(stage: str, current: int, total: int) -> None:
            if stage in reported_stages or stage == "completed":
                return
            reported_stages.add(stage)
            messages = {
                "rendering": "Renderizando páginas…",
                "interpreting": "Interpretando el contenido…",
                "validating": "Validando los datos y generando el Excel…",
            }
            if stage in messages:
                await self._send(adapter, chat_id, messages[stage], thread_id)

        try:
            with tempfile.TemporaryDirectory(prefix="contabot-pdf-xlsx-") as work:
                output, result = await self._convert(document, Path(work), report_progress)
                if len(output.stem) > 64:
                    await self._send(
                        adapter,
                        chat_id,
                        "Aviso: Telegram acortará el nombre del archivo a 64 caracteres.",
                        thread_id,
                    )
                caption = self._success_caption(result)
                delivery = await adapter.send_document(
                    chat_id=str(chat_id),
                    file_path=str(output),
                    file_name=output.name,
                    caption=caption,
                    metadata={"thread_id": thread_id} if thread_id is not None else None,
                )
                if not delivery.success:
                    raise RuntimeError("Telegram no confirmó la entrega del Excel")
                delivered_filename = getattr(delivery, "delivered_filename", None)
                if delivered_filename is not None and delivered_filename != output.name:
                    logger.error(
                        "[PDF-XLSX] stage=delivery status=DELIVERY_FILENAME_MISMATCH expected=%r delivered=%r",
                        output.name,
                        delivered_filename,
                    )
                    return True
        except ConversionFailure as exc:
            logger.warning(
                "[PDF-XLSX] run_id=%s stage=convert status=%s reason=%s",
                exc.run_id,
                exc.status,
                exc.reason,
            )
            await self._send(adapter, chat_id, self._failure_message(exc), thread_id)
            return True
        except Exception as exc:
            logger.exception("[PDF-XLSX] stage=delivery status=ERROR_TECNICO error=%s", type(exc).__name__)
            await self._send(adapter, chat_id, "Ocurrió un error técnico al convertir o entregar el Excel.", thread_id)
            return True
        await self._send(adapter, chat_id, "Excel enviado.", thread_id)
        return True

    async def _convert(
        self,
        document,
        workdir: Path,
        progress: Callable[[str, int, int], Awaitable[None]] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        run_id = uuid.uuid4().hex
        source_dir = self.input_cache_dir / run_id
        source_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        source_dir.chmod(0o700)
        original = Path(str(getattr(document, "file_name", "") or "documento.pdf")).name
        source_name = original if original.lower().endswith(".pdf") else f"{original}.pdf"
        source = source_dir / source_name
        output = workdir / self._xlsx_name(document)
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=source)
        source.chmod(0o600)
        if self.router_project_dir is not None and self.router_project_dir.is_dir():
            return await self._convert_with_router(source, workdir, run_id, progress)
        proc = await asyncio.create_subprocess_exec(
            str(self._converter_python()), "-m", "conversion_documentos.convertir_documento",
            "--input", str(source), "--output", str(output),
            cwd=str(self.project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, stderr = await asyncio.wait_for(
                self._communicate_with_progress(proc, progress),
                timeout=self.document_timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ConversionFailure("VISION_BACKEND_UNAVAILABLE", "DOCUMENT_TIMEOUT", run_id) from None
        result = self._read_result(raw)
        if proc.returncode != 0 or not result.get("ok") or not output.is_file():
            status = str(result.get("status") or "ERROR_TECNICO")
            reason = str(result.get("reason") or f"SUBPROCESS_EXIT_{proc.returncode}")
            logger.warning(
                "[PDF-XLSX] run_id=%s stage=subprocess status=%s reason=%s stderr_present=%s",
                run_id,
                status,
                reason,
                bool(stderr.strip()),
            )
            raise ConversionFailure(status, reason, run_id)
        return output, result

    def _router_command(self, source: Path) -> list[str]:
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
        uv = hermes_home / "bin" / "uv"
        if not uv.is_file():
            raise RuntimeError("ROUTER_RUNTIME_UNAVAILABLE")
        return [
            str(uv), "run", "--with", "pdfplumber", "--with", "openpyxl", "python3",
            "skills/accounting/pdf-contable-router/scripts/router.py", "ingest",
            "--input", str(source),
        ]

    def _cleanup_preserved_staging(self, source: Path) -> None:
        staging = source.parent
        root = self.input_cache_dir
        if (
            root.is_symlink()
            or staging.is_symlink()
            or not root.is_dir()
            or not staging.is_dir()
            or staging.parent != root
            or source.parent != staging
        ):
            logger.error("[PDF-XLSX] stage=staging_cleanup status=CONTAINMENT_REJECTED")
            return
        shutil.rmtree(staging)

    async def _convert_with_router(
        self,
        source: Path,
        workdir: Path,
        run_id: str,
        progress: Callable[[str, int, int], Awaitable[None]] | None,
    ) -> tuple[Path, dict[str, Any]]:
        if progress is not None:
            await progress("interpreting", 1, 1)
        proc = await asyncio.create_subprocess_exec(
            *self._router_command(source),
            cwd=str(self.router_project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.document_timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ConversionFailure("ERROR_TECNICO", "DOCUMENT_TIMEOUT", run_id) from None
        result = self._read_result(raw)
        output_value = result.get("output_path")
        output = Path(output_value) if isinstance(output_value, str) else workdir / "missing.xlsx"
        if proc.returncode != 0 or result.get("status") != "CONVERTED" or not output.is_file():
            status = str(result.get("status") or "ERROR_TECNICO")
            reason = self._router_failure_reason(result, stderr, proc.returncode)
            logger.warning(
                "[PDF-XLSX] run_id=%s stage=router status=%s reason=%s stderr_present=%s",
                run_id, status, reason, bool(stderr.strip()),
            )
            raise ConversionFailure(status, reason, run_id)
        if progress is not None:
            await progress("validating", 1, 1)
        if result.get("reception_preserved") is True:
            self._cleanup_preserved_staging(source)
        return output, result

    async def _communicate_with_progress(
        self,
        proc,
        progress: Callable[[str, int, int], Awaitable[None]] | None,
    ) -> tuple[bytes, bytes]:
        async def read_stdout() -> bytes:
            chunks: list[bytes] = []
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                chunks.append(line)
                if progress is None:
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                    if event.get("event") == "progress":
                        await progress(
                            str(event["stage"]),
                            int(event.get("current") or 0),
                            int(event.get("total") or 0),
                        )
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                except Exception as exc:
                    logger.warning(
                        "[PDF-XLSX] stage=progress status=TELEGRAM_PROGRESS_FAILED error=%s",
                        type(exc).__name__,
                    )
            return b"".join(chunks)

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            await proc.wait()
            return await stdout_task, await stderr_task
        except BaseException:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

    @staticmethod
    def _router_failure_reason(result: dict[str, Any], stderr: bytes, returncode: int | None) -> str:
        reported = result.get("reason")
        if isinstance(reported, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", reported):
            return f"ROUTER_{reported}"
        text = stderr.decode("utf-8", errors="replace")
        matches = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Mismatch|Blocked))\b", text)
        if matches:
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", matches[-1]).upper()
            return f"ROUTER_{normalized}"
        return f"ROUTER_SUBPROCESS_EXIT_{returncode}"

    @staticmethod
    def _success_caption(result: dict[str, Any]) -> str:
        rows = int(result.get("rows_ok") or 0)
        if rows == 0:
            return "Excel generado. No se detectaron movimientos del mes."
        return f"Excel generado: {rows} movimientos del mes."

    @staticmethod
    def _xlsx_name(document: Any) -> str:
        original = Path(str(getattr(document, "file_name", "") or "documento.pdf")).name
        stem = Path(original).stem.strip() or "documento"
        return f"{stem}.xlsx"

    @staticmethod
    def _failure_message(error: ConversionFailure) -> str:
        business_messages = {
            "ROUTER_UNIDENTIFIED": "El documento no corresponde todavía a un emisor reconocido.",
            "ROUTER_IDENTIFIED_NO_ROUTE": "El formato de este documento todavía no está soportado.",
            "ROUTER_LAYOUT_NOT_SUPPORTED": "El formato de este documento todavía no está soportado.",
            "ROUTER_ROUTE_NOT_CONNECTED": "El formato fue reconocido y quedó preservado; su procesamiento aún no está habilitado.",
            "ROUTER_AMBIGUOUS": "El emisor o formato del documento requiere revisión.",
        }
        if error.reason in business_messages:
            return business_messages[error.reason]
        if error.reason.startswith("ROUTER_"):
            return f"El router no pudo procesar el PDF ({error.reason})."
        if error.status == "PDF_CIFRADO":
            return "El PDF está cifrado. Enviá una copia sin contraseña."
        if error.status == "VISION_BACKEND_UNAVAILABLE" and error.reason in {
            "PAGE_TIMEOUT", "DOCUMENT_TIMEOUT", "TimeoutError"
        }:
            return "El servicio de interpretación visual no respondió a tiempo. Volvé a intentar."
        if error.status == "VISION_BACKEND_UNAVAILABLE":
            return "El servicio de interpretación visual no está disponible en este momento."
        if error.status == "NO_TABULAR_DATA":
            return "El PDF no contiene tablas que se puedan convertir."
        return "Ocurrió un error técnico durante la conversión del PDF."

    def _converter_python(self) -> Path:
        python = self.project_dir / ".venv" / "bin" / "python"
        if not python.is_file():
            raise RuntimeError("ENTORNO_DE_CONVERSION_NO_DISPONIBLE")
        return python

    @staticmethod
    def _read_result(raw: bytes) -> dict[str, Any]:
        for line in reversed(raw.decode("utf-8", errors="replace").splitlines()):
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict):
                return result
        return {}
