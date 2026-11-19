"""Flujo Telegram para proteger y retirar restricciones de PDFs."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import aligned_menu_label, menu_label
from plugins.platforms.telegram.contabot_compartido import cargar

# #200: motor único con el panel. Las funciones puras viven en ContaBot (lib/contabot_pdf_seguridad.py),
# publicadas por el despliegue; el panel importa el mismo archivo. No hay copia en el plugin.
_motor = cargar("contabot_pdf_seguridad")
_MAX_DELIVERY_FILENAME = _motor._MAX_DELIVERY_FILENAME
_PASSWORD_RECOVERY_SECONDS = _motor._PASSWORD_RECOVERY_SECONDS
_COMMON_PDF_PASSWORDS = _motor._COMMON_PDF_PASSWORDS
PdfSecurityError = _motor.PdfSecurityError
_delivery_filename = _motor._delivery_filename
_run_qpdf = _motor._run_qpdf
_run_qpdf_argument_file = _motor._run_qpdf_argument_file
_page_count = _motor._page_count
_validate_unlocked_output = _motor._validate_unlocked_output
recover_common_pdf_password = _motor.recover_common_pdf_password
unlock_pdf_without_password = _motor.unlock_pdf_without_password
protect_pdf = _motor.protect_pdf

logger = logging.getLogger(__name__)

_PDF_MIME = "application/pdf"
_STATE_TTL_SECONDS = 15 * 60


@dataclass
class PdfSecurityState:
    operation: str
    stage: str
    nonce: str
    created_at: float
    run_dir: Path | None = None
    source_path: Path | None = None
    received_name: str | None = None


class PdfSecurityFlow:
    """Orquesta Proteger/Desbloquear PDF sin involucrar al agente LLM."""

    def __init__(self, work_root: Path | None = None) -> None:
        self.work_root = work_root or (Path.home() / ".cache" / "hermes" / "pdf-security")
        self.states: dict[tuple[str, str, str], PdfSecurityState] = {}

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> tuple[str, str, str]:
        return str(chat_id), str(thread_id or ""), str(user_id)

    async def _send(self, adapter, chat_id, text: str, *, keyboard=None, thread_id=None) -> None:
        kwargs = {"chat_id": chat_id, "text": text}
        if keyboard is not None:
            kwargs["reply_markup"] = keyboard
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await adapter._bot.send_message(**kwargs)

    def _ensure_root(self) -> None:
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.work_root.lstat()
        if self.work_root.is_symlink() or not self.work_root.is_dir():
            raise PdfSecurityError("WORKDIR_INVALID", "El área privada de trabajo no es segura.")
        if info.st_uid != os.geteuid() or (info.st_mode & 0o077):
            raise PdfSecurityError("WORKDIR_INVALID", "El área privada de trabajo tiene permisos inseguros.")

    def _new_run_dir(self) -> Path:
        self._ensure_root()
        run_dir = self.work_root / uuid.uuid4().hex
        run_dir.mkdir(mode=0o700)
        return run_dir

    def _drop(self, key: tuple[str, str, str]) -> None:
        state = self.states.pop(key, None)
        if state and state.run_dir and state.run_dir.parent == self.work_root:
            shutil.rmtree(state.run_dir, ignore_errors=True)

    def _prune(self) -> None:
        now = time.monotonic()
        for key, state in list(self.states.items()):
            if now - state.created_at > _STATE_TTL_SECONDS:
                self._drop(key)

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id) -> bool:
        self._prune()
        key = self._key(chat_id, thread_id, user_id)
        if data == "ps:protect:start":
            self._drop(key)
            self.states[key] = PdfSecurityState("protect", "document", uuid.uuid4().hex[:10], time.monotonic())
            await query.answer("Proteger PDF")
            await self._send(adapter, chat_id, "Enviá el PDF que querés proteger.", thread_id=thread_id)
            return True
        if data == "ps:unlock:start":
            self._drop(key)
            nonce = uuid.uuid4().hex[:10]
            self.states[key] = PdfSecurityState("unlock", "consent", nonce, time.monotonic())
            keyboard = self._unlock_consent_keyboard(nonce)
            await query.answer("Desbloquear PDF")
            await self._send(
                adapter,
                chat_id,
                "Para continuar, confirmá que tenés autorización para retirar la protección del documento y que asumís la responsabilidad por su uso.",
                keyboard=keyboard,
                thread_id=thread_id,
            )
            return True
        if data.startswith("ps:cancel:"):
            state = self.states.get(key)
            nonce = data.rsplit(":", 1)[-1]
            if not state or state.nonce != nonce:
                await query.answer("Esta operación venció.")
                return True
            self._drop(key)
            await query.answer("Operación cancelada")
            await self._send(adapter, chat_id, "Operación cancelada.", thread_id=thread_id)
            return True
        if data.startswith("ps:unlock:accept:"):
            state = self.states.get(key)
            nonce = data.rsplit(":", 1)[-1]
            if not state or state.operation != "unlock" or state.stage != "consent" or state.nonce != nonce:
                await query.answer("Esta confirmación venció.")
                return True
            state.stage = "document"
            logger.info("[PDF-SECURITY] unlock_authorized run=%s", state.nonce)
            await query.answer("Autorización confirmada")
            await self._send(adapter, chat_id, "Enviá el PDF que querés desbloquear.", thread_id=thread_id)
            return True
        return False

    async def document(self, adapter, message) -> bool:
        self._prune()
        thread_id = getattr(message, "message_thread_id", None)
        key = self._key(message.chat_id, thread_id, message.from_user.id)
        state = self.states.get(key)
        if not state or state.stage != "document":
            return False
        document = message.document
        name = Path(document.file_name or "").name
        mime = (document.mime_type or "").lower()
        if not name.lower().endswith(".pdf") or mime not in {_PDF_MIME, "application/octet-stream", ""}:
            await self._send(adapter, message.chat_id, "Necesito un archivo PDF.", thread_id=thread_id)
            return True
        max_bytes = int(getattr(adapter, "_max_doc_bytes", 50 * 1024 * 1024))
        if not document.file_size or document.file_size > max_bytes:
            await self._send(adapter, message.chat_id, "El PDF supera el tamaño permitido o Telegram no informó su tamaño.", thread_id=thread_id)
            return True
        try:
            file_obj = await document.get_file()
            raw = bytes(await file_obj.download_as_bytearray())
            if len(raw) != document.file_size or not raw.startswith(b"%PDF-"):
                raise PdfSecurityError("INVALID_PDF", "El archivo recibido no es un PDF válido.")
            run_dir = self._new_run_dir()
            source = run_dir / "source.pdf"
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            state.run_dir = run_dir
            state.source_path = source
            state.received_name = name
            if state.operation == "protect":
                state.stage = "password"
                await self._send(
                    adapter,
                    message.chat_id,
                    "Escribí la contraseña de apertura. Intentaré eliminar el mensaje del chat y la contraseña no se guardará.",
                    thread_id=thread_id,
                )
                return True
            await self._process_unlock(adapter, message.chat_id, thread_id, key, state)
            return True
        except PdfSecurityError as exc:
            self._drop(key)
            await self._send(adapter, message.chat_id, str(exc), thread_id=thread_id)
            return True
        except Exception:
            logger.exception("[PDF-SECURITY] document preparation failed")
            self._drop(key)
            await self._send(adapter, message.chat_id, "No se pudo preparar el PDF. No se generó ningún archivo.", thread_id=thread_id)
            return True

    async def text(self, adapter, message) -> bool:
        self._prune()
        thread_id = getattr(message, "message_thread_id", None)
        key = self._key(message.chat_id, thread_id, message.from_user.id)
        state = self.states.get(key)
        if not state or state.operation != "protect" or state.stage != "password":
            return False
        password = message.text or ""
        password_message_deleted = await adapter.delete_message(str(message.chat_id), str(message.message_id))
        try:
            if not state.source_path or not state.run_dir or not state.received_name:
                raise PdfSecurityError("STATE_INVALID", "La operación venció. Volvé a iniciarla.")
            output = state.run_dir / "output.pdf"
            await asyncio.to_thread(protect_pdf, state.source_path, output, password)
            await self._deliver(
                adapter,
                message.chat_id,
                thread_id,
                output,
                _delivery_filename(state.received_name, "protegido"),
                "PDF protegido con contraseña de apertura.",
            )
            if not password_message_deleted:
                await self._send(
                    adapter,
                    message.chat_id,
                    "No pude eliminar el mensaje que contenía la contraseña. Borrálo manualmente del chat.",
                    thread_id=thread_id,
                )
        except PdfSecurityError as exc:
            await self._send(adapter, message.chat_id, str(exc), thread_id=thread_id)
        except Exception:
            logger.exception("[PDF-SECURITY] protection failed")
            await self._send(adapter, message.chat_id, "No se pudo proteger el PDF. No se generó ningún archivo.", thread_id=thread_id)
        finally:
            password = ""
            self._drop(key)
        return True

    async def _process_unlock(self, adapter, chat_id, thread_id, key, state) -> None:
        try:
            if not state.source_path or not state.run_dir or not state.received_name:
                raise PdfSecurityError("STATE_INVALID", "La operación venció. Volvé a iniciarla.")
            output = state.run_dir / "output.pdf"
            await self._send(
                adapter,
                chat_id,
                "Estoy verificando la protección e intentando recuperar una contraseña común si fuera necesario.",
                thread_id=thread_id,
            )
            await asyncio.to_thread(unlock_pdf_without_password, state.source_path, output)
            await self._deliver(
                adapter,
                chat_id,
                thread_id,
                output,
                _delivery_filename(state.received_name, "desbloqueado"),
                "PDF entregado sin bloqueo por contraseña ni restricciones de uso.",
            )
        except PdfSecurityError as exc:
            await self._send(adapter, chat_id, str(exc), thread_id=thread_id)
        except Exception:
            logger.exception("[PDF-SECURITY] unlock failed")
            await self._send(adapter, chat_id, "No se pudo desbloquear el PDF. No se generó ningún archivo.", thread_id=thread_id)
        finally:
            self._drop(key)

    async def _deliver(self, adapter, chat_id, thread_id, output: Path, name: str, caption: str) -> None:
        result = await adapter.send_document(
            chat_id=str(chat_id),
            file_path=str(output),
            file_name=name,
            caption=caption,
            metadata={"thread_id": thread_id} if thread_id is not None else None,
        )
        if not result.success:
            raise PdfSecurityError("DELIVERY_FAILED", "Telegram no confirmó la entrega del PDF.")

    @staticmethod
    def _unlock_consent_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(menu_label("✅", "Acepto y continuar"), callback_data=f"ps:unlock:accept:{nonce}")],
                [InlineKeyboardButton(aligned_menu_label("pdf_consentimiento", "❌", "Cancelar"), callback_data=f"ps:cancel:{nonce}")],
            ]
        )
