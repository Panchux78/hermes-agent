import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.pdf_security_flow import (
    PdfSecurityError,
    PdfSecurityFlow,
    _delivery_filename,
    protect_pdf,
    unlock_pdf_without_password,
)
from plugins.platforms.telegram.adapter import TelegramAdapter


def _plain_pdf(tmp_path: Path) -> Path:
    source = tmp_path / "plain.pdf"
    fixture = Path("docs/hermes-kanban-v1-spec.pdf").resolve()
    subprocess.run(
        ["qpdf", "--empty", "--pages", str(fixture), "1", "--", str(source)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return source


def _qpdf(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["qpdf", *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def test_protect_and_unlock_contract_distinguishes_real_open_password(tmp_path):
    source = _plain_pdf(tmp_path)
    protected = tmp_path / "protected.pdf"

    protect_pdf(source, protected, "clave-segura", tmp_path)

    assert _qpdf("--requires-password", str(protected)).returncode == 0
    with pytest.raises(PdfSecurityError, match="contraseña real de apertura") as caught:
        unlock_pdf_without_password(protected, tmp_path / "unlocked.pdf")
    assert caught.value.code == "OPEN_PASSWORD_REQUIRED"
    assert not (tmp_path / ".qpdf-args").exists()
    assert not (tmp_path / ".qpdf-check-args").exists()


def test_unlock_removes_owner_restrictions_without_requesting_a_password(tmp_path):
    source = _plain_pdf(tmp_path)
    restricted = tmp_path / "restricted.pdf"
    output = tmp_path / "unlocked.pdf"
    subprocess.run(
        [
            "qpdf",
            "--encrypt",
            "--user-password=",
            "--owner-password=owner-secret",
            "--bits=256",
            "--print=none",
            "--modify=none",
            "--",
            str(source),
            str(restricted),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    unlock_pdf_without_password(restricted, output)

    assert _qpdf("--requires-password", str(output)).returncode == 2
    assert (output.stat().st_mode & 0o777) == 0o600
    assert _qpdf("--show-npages", str(output)).stdout.strip() == "1"


def test_delivery_filename_preserves_functional_suffix_inside_telegram_limit():
    name = "a" * 100 + ".pdf"

    actual = _delivery_filename(name, "desbloqueado")

    assert len(actual) == 64
    assert actual.endswith("-desbloqueado.pdf")


def test_unlock_requires_explicit_authorization_before_accepting_pdf(tmp_path):
    async def scenario():
        flow = PdfSecurityFlow(work_root=tmp_path / "private")
        bot = SimpleNamespace(send_message=AsyncMock())
        adapter = SimpleNamespace(_bot=bot)
        query = SimpleNamespace(answer=AsyncMock())

        assert await flow.callback(adapter, query, "ps:unlock:start", 1, None, "2")
        state = flow.states[("1", "", "2")]
        assert state.stage == "consent"
        text = bot.send_message.await_args.kwargs["text"]
        assert "autorización" in text
        assert "responsabilidad" in text

        query2 = SimpleNamespace(answer=AsyncMock())
        assert await flow.callback(
            adapter, query2, f"ps:unlock:accept:{state.nonce}", 1, None, "2"
        )
        assert state.stage == "document"

    asyncio.run(scenario())


def test_protect_password_message_is_deleted_and_never_persisted(monkeypatch, tmp_path):
    async def scenario():
        flow = PdfSecurityFlow(work_root=tmp_path / "private")
        run_dir = flow._new_run_dir()
        source = _plain_pdf(run_dir)
        key = ("1", "", "2")
        from plugins.platforms.telegram.pdf_security_flow import PdfSecurityState

        flow.states[key] = PdfSecurityState(
            operation="protect",
            stage="password",
            nonce="abc",
            created_at=0.0,
            run_dir=run_dir,
            source_path=source,
            received_name="documento.pdf",
        )
        flow.states[key].created_at = __import__("time").monotonic()
        adapter = SimpleNamespace(
            _bot=SimpleNamespace(send_message=AsyncMock()),
            delete_message=AsyncMock(return_value=True),
            send_document=AsyncMock(return_value=SimpleNamespace(success=True)),
        )
        message = SimpleNamespace(
            chat_id=1,
            message_thread_id=None,
            message_id=44,
            from_user=SimpleNamespace(id=2),
            text="clave-segura",
        )

        assert await flow.text(adapter, message)

        adapter.delete_message.assert_awaited_once_with("1", "44")
        adapter.send_document.assert_awaited_once()
        assert key not in flow.states
        assert not run_dir.exists()
        assert not any("clave-segura" in str(call) for call in adapter._bot.send_message.await_args_list)

    asyncio.run(scenario())


def test_private_workdir_with_broad_permissions_is_rejected(tmp_path):
    root = tmp_path / "unsafe"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    with pytest.raises(PdfSecurityError) as caught:
        PdfSecurityFlow(work_root=root)._new_run_dir()

    assert caught.value.code == "WORKDIR_INVALID"


def test_adapter_routes_security_callback_only_for_authorized_user():
    async def scenario():
        adapter = object.__new__(TelegramAdapter)
        adapter._is_callback_user_authorized = lambda *args, **kwargs: True
        adapter._pdf_security_flow = SimpleNamespace(callback=AsyncMock(return_value=True))
        query = SimpleNamespace(
            data="ps:unlock:start",
            from_user=SimpleNamespace(id=2, first_name="Test"),
            message=SimpleNamespace(
                chat_id=1,
                chat=SimpleNamespace(type="private"),
                message_thread_id=None,
            ),
            answer=AsyncMock(),
        )

        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

        adapter._pdf_security_flow.callback.assert_awaited_once_with(
            adapter, query, "ps:unlock:start", 1, None, "2"
        )

    asyncio.run(scenario())


def test_adapter_gives_pending_security_flow_first_access_to_pdf():
    async def scenario():
        adapter = object.__new__(TelegramAdapter)
        adapter._is_user_authorized_from_message = lambda message: True
        adapter._should_process_message = lambda message: True
        adapter._pdf_security_flow = SimpleNamespace(document=AsyncMock(return_value=True))
        adapter._batch_pdf_xlsx_flow = SimpleNamespace(document=AsyncMock(return_value=True))
        adapter._pdf_xlsx_flow = SimpleNamespace(document=AsyncMock(return_value=True))
        message = SimpleNamespace(document=SimpleNamespace(), from_user=SimpleNamespace(id=2), chat=SimpleNamespace(id=1))

        await adapter._handle_media_message(SimpleNamespace(message=message, update_id=3), SimpleNamespace())

        adapter._pdf_security_flow.document.assert_awaited_once_with(adapter, message)
        adapter._batch_pdf_xlsx_flow.document.assert_not_awaited()
        adapter._pdf_xlsx_flow.document.assert_not_awaited()

    asyncio.run(scenario())
