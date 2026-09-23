"""Flujo Telegram autorizado para convertir un PDF recibido a XLSX."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)
_DEFAULT_PROJECT_DIR = Path("/home/pancho/hermes-workspace/conversion-documentos-contables-xlsx")
_DEFAULT_ROUTER_PROJECT_DIR = Path("/home/pancho/hermes-workspace/Contabot")
_MAX_PDF_BYTES = 5_000_000
# Un pedido del menú de bancos («mandame el PDF») que no se completa vence solo.
# Sin vencimiento, un pedido olvidado de una opción se quedaba con los archivos
# que el usuario mandaba después para otra (caso real del 22/09/2026).
PENDING_REQUEST_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class PdfXlsxRequest:
    user_id: str
    created_at: float = field(default_factory=lambda: time.monotonic())


def pending_request(requests: dict, key: str, *, flow: str):
    """El pedido pendiente de `key`, o None si no hay o ya venció (y lo descarta)."""
    request = requests.get(key)
    if request is None:
        return None
    if time.monotonic() - request.created_at > PENDING_REQUEST_TTL_SECONDS:
        requests.pop(key, None)
        logger.info("[%s] stage=request status=EXPIRED user=%s", flow, request.user_id)
        return None
    return request


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
        self.instance_id = str(uuid.uuid4())
        self._history_recovered = False
        self._history_lock = asyncio.Lock()

    @staticmethod
    def _positive_float(value: str | None, default: float) -> float:
        try:
            parsed = float(value) if value is not None else default
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _delivery_filename(output: Path) -> str:
        if len(output.name) <= 64:
            return output.name
        stem, extension = output.stem, output.suffix
        versioned = re.fullmatch(r"(?P<base>.*)(?P<version>-v\d+)", stem)
        if versioned:
            version = versioned.group("version")
            budget = 64 - len(version) - len(extension)
            return f"{versioned.group('base')[:budget]}{version}{extension}"
        return f"{stem[:64 - len(extension)]}{extension}"

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    async def _send(adapter, chat_id, text: str, thread_id=None) -> None:
        kwargs = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await adapter._bot.send_message(**kwargs)

    def cancel_pending(self, chat_id, thread_id, user_id) -> bool:
        """Descarta el pedido pendiente: el usuario eligió otra opción del menú."""
        request = self.requests.pop(self._key(chat_id, thread_id, user_id), None)
        if request is not None:
            logger.info("[PDF-XLSX] stage=request status=CANCELLED_BY_OTHER_OPTION user=%s", request.user_id)
        return request is not None

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id) -> bool:
        if data != "px:start":
            return False
        self.requests[self._key(chat_id, thread_id, user_id)] = PdfXlsxRequest(user_id=str(user_id))
        await query.answer("Resumen bancario → Excel")
        await self._send(
            adapter,
            chat_id,
            "Mandame el resumen bancario que querés convertir a Excel.",
            thread_id,
        )
        return True

    async def document(self, adapter, message) -> bool:
        chat_id = message.chat_id
        thread_id = getattr(message, "message_thread_id", None)
        user_id = str(getattr(message.from_user, "id", ""))
        key = self._key(chat_id, thread_id, user_id)
        if pending_request(self.requests, key, flow="PDF-XLSX") is None:
            return False
        document = getattr(message, "document", None)
        name = str(getattr(document, "file_name", "") or "").lower()
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
        size = int(getattr(document, "file_size", 0) or 0)
        if not document or not (name.endswith(".pdf") or mime_type == "application/pdf"):
            logger.info("[PDF-XLSX] stage=document status=REJECTED reason=not_pdf user=%s", user_id)
            await self._send(adapter, chat_id, "Esperaba un archivo PDF. Mandá el PDF para convertirlo a Excel.", thread_id)
            return True
        if size <= 0 or size > _MAX_PDF_BYTES:
            logger.info("[PDF-XLSX] stage=document status=REJECTED reason=size bytes=%s user=%s", size, user_id)
            await self._send(adapter, chat_id, "El PDF supera el límite de 5 MB o Telegram no informó su tamaño.", thread_id)
            return True

        self.requests.pop(key, None)
        history: dict[str, int] | None = None
        if self.router_project_dir is not None:
            try:
                history = await self._history_start(message, document)
            except Exception as exc:
                logger.error(
                    "[PDF-XLSX] stage=history_start status=ERROR error=%s",
                    type(exc).__name__,
                )
                await self._send(
                    adapter,
                    chat_id,
                    "No pude registrar la operación. No se inició la conversión; requiere mantenimiento local.",
                    thread_id,
                )
                return True
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

        output: Path | None = None
        result: dict[str, Any] | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="contabot-pdf-xlsx-") as work:
                output, result = await self._convert(document, Path(work), report_progress)
                if history is not None:
                    await self._history_attach(history, output, result)
                delivery_name = self._delivery_filename(output)
                if delivery_name != output.name:
                    await self._send(
                        adapter,
                        chat_id,
                        "Aviso: por el límite de Telegram, el archivo se enviará con un nombre acortado a 64 caracteres.",
                        thread_id,
                    )
                caption = self._success_caption(result)
                delivery = await adapter.send_document(
                    chat_id=str(chat_id),
                    file_path=str(output),
                    file_name=delivery_name,
                    caption=caption,
                    metadata={"thread_id": thread_id} if thread_id is not None else None,
                )
                if not delivery.success:
                    raise RuntimeError("Telegram no confirmó la entrega del Excel")
                delivered_filename = getattr(delivery, "delivered_filename", None)
                if delivered_filename is not None and delivered_filename != delivery_name:
                    logger.error(
                        "[PDF-XLSX] stage=delivery status=DELIVERY_FILENAME_MISMATCH expected=%r delivered=%r",
                        delivery_name,
                        delivered_filename,
                    )
                    if history is not None:
                        await self._history_finish(
                            history,
                            state="incompleto",
                            reason_code="nombre_entregado_distinto",
                            reason_text="Telegram devolvió un nombre de archivo distinto del enviado.",
                            result=result,
                            converted=True,
                        )
                    return True
                incomplete = result.get("conversion_incomplete") is True
                if history is not None:
                    await self._history_finish(
                        history,
                        state="incompleto" if incomplete else "completado",
                        reason_code="saldos_no_cierran" if incomplete else "conversion_completada",
                        reason_text=(
                            "La planilla fue generada, pero la cadena de saldos no cierra."
                            if incomplete else "La conversión y la entrega finalizaron correctamente."
                        ),
                        result=result,
                        converted=True,
                    )
        except asyncio.CancelledError:
            if history is not None:
                try:
                    await asyncio.shield(self._history_finish(
                        history,
                        state="cancelado",
                        reason_code="cancelado_por_usuario",
                        reason_text="La operación fue cancelada antes de finalizar.",
                        result=result,
                        converted=False,
                    ))
                except Exception:
                    logger.exception("[PDF-XLSX] stage=history_cancel status=ERROR")
            raise
        except ConversionFailure as exc:
            logger.warning(
                "[PDF-XLSX] run_id=%s stage=convert status=%s reason=%s",
                exc.run_id,
                exc.status,
                exc.reason,
            )
            if history is not None:
                await self._history_finish(
                    history,
                    state="fallido",
                    reason_code=self._history_reason_code(exc.reason),
                    reason_text=self._failure_message(exc),
                    result=result,
                    converted=False,
                )
            await self._send(adapter, chat_id, self._failure_message(exc), thread_id)
            return True
        except Exception as exc:
            logger.exception("[PDF-XLSX] stage=delivery status=ERROR_TECNICO error=%s", type(exc).__name__)
            try:
                if history is not None:
                    await self._history_finish(
                    history,
                    state="incompleto" if output is not None else "fallido",
                    reason_code="entrega_no_confirmada" if output is not None else "error_tecnico",
                    reason_text=(
                        "El Excel fue generado, pero Telegram no confirmó su entrega."
                        if output is not None else "La conversión terminó por un error técnico."
                    ),
                    result=result,
                    converted=output is not None,
                )
            except Exception:
                logger.exception("[PDF-XLSX] stage=history_finish status=ERROR")
            await self._send(adapter, chat_id, "Ocurrió un error técnico al convertir o entregar el Excel.", thread_id)
            return True
        await self._send(adapter, chat_id, "Excel enviado.", thread_id)
        return True

    def _history_script(self) -> Path:
        if self.router_project_dir is None:
            raise RuntimeError("HISTORY_RUNTIME_UNAVAILABLE")
        script = self.router_project_dir / "skills/accounting/pdf-contable-router/scripts/bot_runs.py"
        if script.is_symlink() or not script.is_file():
            raise RuntimeError("HISTORY_RUNTIME_UNAVAILABLE")
        return script

    async def _history_call(self, *arguments: str) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self._history_script()),
            *arguments,
            cwd=str(self.router_project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("HISTORY_TIMEOUT") from None
        payload = self._read_result(raw)
        if proc.returncode != 0 or payload.get("status") != "OK":
            raise RuntimeError("HISTORY_UNAVAILABLE")
        return payload

    async def _recover_history_once(self) -> None:
        if self._history_recovered:
            return
        async with self._history_lock:
            if self._history_recovered:
                return
            await self._history_call("recover", "--instance", self.instance_id)
            self._history_recovered = True

    async def _history_start(self, message, document) -> dict[str, int]:
        await self._recover_history_once()
        message_id = getattr(message, "message_id", None)
        telegram_id = getattr(message.from_user, "id", None)
        if not isinstance(message_id, int) or not isinstance(telegram_id, int):
            raise RuntimeError("HISTORY_IDENTITY_UNAVAILABLE")
        key = hashlib.sha256(f"telegram:{message.chat_id}:{message_id}".encode()).hexdigest()
        payload = await self._history_call(
            "start",
            "--telegram-id", str(telegram_id),
            "--operation", "resumen_bancario_xlsx",
            "--idempotency-key", key,
            "--instance", self.instance_id,
            "--reference", Path(str(getattr(document, "file_name", "documento.pdf"))).name,
            "--lease-seconds", str(int(self.document_timeout_seconds) + 120),
        )
        run_id, item_id = payload.get("id_corrida"), payload.get("id_item")
        if not isinstance(run_id, int) or not isinstance(item_id, int):
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        return {"id_corrida": run_id, "id_item": item_id}

    async def _history_attach(self, history: dict[str, int], output: Path, result: dict[str, Any]) -> None:
        contributor = result.get("id_contribuyente")
        relative = result.get("output_relative_to_clientes")
        if not isinstance(contributor, int) or not isinstance(relative, str):
            raise RuntimeError("HISTORY_RESULT_IDENTITY_INVALID")
        await self._history_call(
            "attach",
            "--run-id", str(history["id_corrida"]),
            "--item-id", str(history["id_item"]),
            "--instance", self.instance_id,
            "--contributor-id", str(contributor),
            "--output", str(output),
            "--output-relative", relative,
        )

    async def _history_finish(
        self,
        history: dict[str, int],
        *,
        state: str,
        reason_code: str,
        reason_text: str,
        result: dict[str, Any] | None,
        converted: bool,
    ) -> None:
        args = [
            "finish",
            "--run-id", str(history["id_corrida"]),
            "--item-id", str(history["id_item"]),
            "--instance", self.instance_id,
            "--state", state,
            "--reason-code", reason_code,
            "--reason-text", reason_text,
            "--effects-json", json.dumps([
                {"codigo": "convertir", "realizado": converted},
                {"codigo": "presentar", "realizado": False, "detalle": "Esta operación no presenta declaraciones."},
                {"codigo": "pagar", "realizado": False, "detalle": "Esta operación no realiza pagos."},
            ], ensure_ascii=False, separators=(",", ":")),
        ]
        if result is not None:
            if isinstance(result.get("id_contribuyente"), int):
                args += ["--contributor-id", str(result["id_contribuyente"])]
            if isinstance(result.get("periodo"), str) and result["periodo"]:
                args += ["--period", result["periodo"]]
            if isinstance(result.get("rows_ok"), int):
                args += ["--delivered-count", str(result["rows_ok"])]
        await self._history_call(*args)

    @staticmethod
    def _history_reason_code(reason: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", reason.lower()).strip("_")
        return normalized[:80] if len(normalized) >= 3 else "error_conversion"

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
        if isinstance(reported, str) and (
            re.fullmatch(r"[a-z][a-z0-9_]{2,80}", reported)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", reported)
        ):
            return f"ROUTER_{reported.upper()}"
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
