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

logger = logging.getLogger(__name__)

_PDF_MIME = "application/pdf"
_STATE_TTL_SECONDS = 15 * 60
_MAX_DELIVERY_FILENAME = 64
_PASSWORD_RECOVERY_SECONDS = 15
# Lista local original seguida por las entradas no duplicadas de:
# SecLists/Passwords/Common-Credentials/xato-net-10-million-passwords-100.txt
# https://github.com/danielmiessler/SecLists (licencia MIT)
_COMMON_PDF_PASSWORDS = (
    "password",
    "Password",
    "PASSWORD",
    "contraseña",
    "Contrasena",
    "1234",
    "12345",
    "123456",
    "1234567",
    "12345678",
    "123456789",
    "1234567890",
    "0000",
    "1111",
    "1212",
    "4321",
    "987654",
    "qwerty",
    "qwerty123",
    "abc123",
    "admin",
    "admin123",
    "welcome",
    "Welcome1",
    "letmein",
    "iloveyou",
    "secret",
    "documento",
    "Documento",
    "empresa",
    "Empresa",
    "contador",
    "Contador",
    "contable",
    "argentina",
    "Argentina",
    "111111",
    "dragon",
    "123123",
    "baseball",
    "football",
    "monkey",
    "696969",
    "shadow",
    "master",
    "666666",
    "qwertyuiop",
    "123321",
    "mustang",
    "michael",
    "654321",
    "pussy",
    "superman",
    "1qaz2wsx",
    "7777777",
    "fuckyou",
    "121212",
    "000000",
    "qazwsx",
    "123qwe",
    "killer",
    "trustno1",
    "jordan",
    "jennifer",
    "zxcvbnm",
    "asdfgh",
    "hunter",
    "buster",
    "soccer",
    "harley",
    "batman",
    "andrew",
    "tigger",
    "sunshine",
    "fuckme",
    "2000",
    "charlie",
    "robert",
    "thomas",
    "hockey",
    "ranger",
    "daniel",
    "starwars",
    "klaster",
    "112233",
    "george",
    "asshole",
    "computer",
    "michelle",
    "jessica",
    "pepper",
    "zxcvbn",
    "555555",
    "11111111",
    "131313",
    "freedom",
    "777777",
    "pass",
    "fuck",
    "maggie",
    "159753",
    "aaaaaa",
    "ginger",
    "princess",
    "joshua",
    "cheese",
    "amanda",
    "summer",
    "love",
    "ashley",
    "6969",
    "nicole",
    "chelsea",
    "biteme",
    "matthew",
    "access",
    "yankees",
    "987654321",
    "dallas",
    "austin",
    "thunder",
    "taylor",
)


class PdfSecurityError(RuntimeError):
    """Error de negocio saneado del flujo de seguridad PDF."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class PdfSecurityState:
    operation: str
    stage: str
    nonce: str
    created_at: float
    run_dir: Path | None = None
    source_path: Path | None = None
    received_name: str | None = None


def _delivery_filename(received_name: str, suffix: str) -> str:
    """Preserva el sufijo funcional dentro del límite observado de Telegram."""
    safe_name = Path(received_name).name
    stem = Path(safe_name).stem
    tail = f"-{suffix}.pdf"
    available = _MAX_DELIVERY_FILENAME - len(tail)
    if available < 1:
        raise PdfSecurityError("INVALID_FILENAME", "El nombre del PDF no es válido.")
    return f"{stem[:available]}{tail}"


def _run_qpdf(
    args: list[str], *, password: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["qpdf", *args],
        input=f"{password}\n" if password is not None else None,
        stdin=subprocess.DEVNULL if password is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_qpdf_argument_file(lines: list[str]) -> subprocess.CompletedProcess[str]:
    """Entrega argumentos sensibles por un pipe heredado, no por argv o disco."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, ("\n".join([*lines, ""])).encode("utf-8"))
    finally:
        os.close(write_fd)
    try:
        return subprocess.run(
            ["qpdf", f"@/proc/self/fd/{read_fd}"],
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        os.close(read_fd)


def _page_count(path: Path, password: str | None = None) -> int:
    args = ["--warning-exit-0", "--show-npages", str(path)]
    if password is not None:
        args.insert(0, "--password-file=/dev/stdin")
    run = _run_qpdf(args, password=password)
    if run.returncode != 0:
        raise PdfSecurityError("INVALID_PDF", "El archivo no es un PDF procesable.")
    try:
        pages = int(run.stdout.strip())
    except ValueError as exc:
        raise PdfSecurityError("INVALID_PDF", "No se pudo verificar la cantidad de páginas.") from exc
    if pages < 1:
        raise PdfSecurityError("INVALID_PDF", "El PDF no contiene páginas.")
    return pages


def _validate_unlocked_output(path: Path, expected_pages: int) -> None:
    check = _run_qpdf(["--warning-exit-0", "--check", str(path)])
    if check.returncode != 0 or not path.is_file():
        raise PdfSecurityError("OUTPUT_INVALID", "El PDF resultante no superó la validación.")
    password_status = _run_qpdf(["--requires-password", str(path)])
    if password_status.returncode != 2:
        raise PdfSecurityError("OUTPUT_STILL_ENCRYPTED", "El PDF resultante continúa cifrado.")
    if _page_count(path) != expected_pages:
        raise PdfSecurityError("PAGE_COUNT_MISMATCH", "El PDF resultante no conserva todas las páginas.")


def recover_common_pdf_password(source: Path) -> str | None:
    """Prueba un conjunto fijo y acotado sin exponer candidatos fuera del proceso."""
    deadline = time.monotonic() + _PASSWORD_RECOVERY_SECONDS
    for candidate in _COMMON_PDF_PASSWORDS:
        if time.monotonic() >= deadline:
            break
        run = _run_qpdf(
            ["--password-file=/dev/stdin", "--requires-password", str(source)],
            password=candidate,
            timeout=5,
        )
        if run.returncode == 3:
            return candidate
        if run.returncode not in {0, 2}:
            raise PdfSecurityError("PASSWORD_CHECK_FAILED", "No se pudo verificar la protección del PDF.")
    return None


def unlock_pdf_without_password(source: Path, output: Path) -> None:
    """Retira restricciones y recupera contraseñas comunes de forma acotada."""
    password_status = _run_qpdf(["--requires-password", str(source)])
    recovered_password: str | None = None
    if password_status.returncode == 0:
        recovered_password = recover_common_pdf_password(source)
        if recovered_password is None:
            raise PdfSecurityError(
                "PASSWORD_RECOVERY_FAILED",
                "El PDF exige una contraseña de apertura y no pude recuperarla con el método automático.",
            )
    elif password_status.returncode not in {2, 3}:
        raise PdfSecurityError("INVALID_PDF", "No se pudo determinar el estado de protección del PDF.")

    expected_pages = _page_count(source, recovered_password)
    args = ["--warning-exit-0", "--decrypt", "--remove-restrictions", str(source), str(output)]
    if recovered_password is not None:
        args.insert(0, "--password-file=/dev/stdin")
    run = _run_qpdf(args, password=recovered_password)
    if run.returncode != 0:
        raise PdfSecurityError("UNLOCK_FAILED", "No se pudo retirar la protección del PDF.")
    os.chmod(output, 0o600)
    _validate_unlocked_output(output, expected_pages)


def protect_pdf(source: Path, output: Path, password: str) -> None:
    """Cifra con AES-256 sin exponer la contraseña en argv ni logs."""
    if not password or "\n" in password or "\r" in password or "\x00" in password:
        raise PdfSecurityError("INVALID_PASSWORD", "La contraseña no puede estar vacía ni contener saltos de línea.")
    if len(password) > 128:
        raise PdfSecurityError("INVALID_PASSWORD", "La contraseña no puede superar 128 caracteres.")

    password_status = _run_qpdf(["--requires-password", str(source)])
    if password_status.returncode == 0:
        raise PdfSecurityError("SOURCE_PASSWORD_REQUIRED", "El PDF de origen ya exige una contraseña de apertura.")
    if password_status.returncode not in {2, 3}:
        raise PdfSecurityError("INVALID_PDF", "No se pudo determinar el estado del PDF.")
    expected_pages = _page_count(source)

    owner_password = secrets.token_urlsafe(32)
    run = _run_qpdf_argument_file(
        [
            "--encrypt",
            f"--user-password={password}",
            f"--owner-password={owner_password}",
            "--bits=256",
            "--",
            str(source),
            str(output),
        ]
    )

    if run.returncode != 0:
        raise PdfSecurityError("PROTECT_FAILED", "No se pudo proteger el PDF con la contraseña indicada.")
    os.chmod(output, 0o600)
    check = _run_qpdf(["--requires-password", str(output)])
    if check.returncode != 0:
        raise PdfSecurityError("OUTPUT_NOT_PROTECTED", "El PDF resultante no quedó protegido por contraseña.")
    if _page_count(output, password) != expected_pages:
        raise PdfSecurityError("OUTPUT_INVALID", "El PDF protegido no superó la validación.")


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
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Acepto y continuar", callback_data=f"ps:unlock:accept:{nonce}")],
                    [InlineKeyboardButton("Cancelar", callback_data=f"ps:cancel:{nonce}")],
                ]
            )
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
