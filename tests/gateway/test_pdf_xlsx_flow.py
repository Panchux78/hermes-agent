import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.pdf_xlsx_flow import ConversionFailure, PdfXlsxFlow


def test_start_then_matching_pdf_starts_conversion_and_delivers_result(monkeypatch, tmp_path):
    async def scenario():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()), send_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock())
        message = SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            from_user=SimpleNamespace(id=99),
            document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=10),
        )
        delivered = tmp_path / "resumen.xlsx"
        delivered.write_bytes(b"xlsx")
        monkeypatch.setattr(flow, "_convert", AsyncMock(return_value=(delivered, {"rows_ok": 6})))
        adapter.send_document.return_value = SimpleNamespace(success=True, message_id="77")

        assert await flow.callback(adapter, query, "px:start", 123, None, "99") is True
        assert await flow.document(adapter, message) is True

        query.answer.assert_awaited_once_with("Convertir PDF a Excel")
        adapter.send_document.assert_awaited_once()
        assert adapter.send_document.await_args.kwargs["file_path"] == str(delivered)
        assert adapter.send_document.await_args.kwargs["file_name"] == "resumen.xlsx"
        assert "6 movimientos" in adapter.send_document.await_args.kwargs["caption"]

    asyncio.run(scenario())


def test_delivery_filename_mismatch_is_reported_as_failure(monkeypatch, tmp_path):
    async def scenario():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()), send_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock())
        message = SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            from_user=SimpleNamespace(id=99),
            document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=10),
        )
        delivered = tmp_path / "resumen-v03.xlsx"
        delivered.write_bytes(b"xlsx")
        monkeypatch.setattr(flow, "_convert", AsyncMock(return_value=(delivered, {"rows_ok": 6})))
        adapter.send_document.return_value = SimpleNamespace(success=True, message_id="77", delivered_filename="resumen_v.xlsx")

        await flow.callback(adapter, query, "px:start", 123, None, "99")
        assert await flow.document(adapter, message) is True

        texts = [call.kwargs["text"] for call in adapter._bot.send_message.await_args_list]
        assert texts[-1] == "El archivo se entregó con un nombre distinto al generado."

    asyncio.run(scenario())


def test_pending_conversion_ignores_other_user_document(tmp_path):
    async def scenario():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()))
        query = SimpleNamespace(answer=AsyncMock())
        await flow.callback(adapter, query, "px:start", 123, None, "99")
        message = SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            from_user=SimpleNamespace(id=100),
            document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=10),
        )
        assert await flow.document(adapter, message) is False

    asyncio.run(scenario())


def test_converter_uses_the_project_venv_python(tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    assert PdfXlsxFlow(project_dir=tmp_path)._converter_python() == python


def test_router_command_uses_the_contabot_router_project(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTA_PDF_ROUTER_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    uv = tmp_path / "hermes-home" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.touch()

    flow = PdfXlsxFlow(project_dir=tmp_path / "legacy-project")

    assert flow._router_command(tmp_path / "documento.pdf") == [
        str(uv), "run", "--with", "pdfplumber", "--with", "openpyxl", "python3",
        "skills/accounting/pdf-contable-router/scripts/router.py", "ingest",
        "--input", str(tmp_path / "documento.pdf"),
    ]


def test_failed_conversion_preserves_received_pdf_for_retry(monkeypatch, tmp_path):
    async def scenario():
        cache = tmp_path / "private-cache"
        monkeypatch.setenv("CONTA_PDF_XLSX_INPUT_CACHE_DIR", str(cache))
        flow = PdfXlsxFlow(project_dir=tmp_path / "missing-project")
        workdir = tmp_path / "work"
        workdir.mkdir()

        async def download_to_drive(custom_path):
            Path(custom_path).write_bytes(b"%PDF-original")

        document = SimpleNamespace(
            file_name="mi resumen.pdf",
            get_file=AsyncMock(return_value=SimpleNamespace(download_to_drive=download_to_drive)),
        )

        with pytest.raises(RuntimeError, match="ENTORNO_DE_CONVERSION_NO_DISPONIBLE"):
            await flow._convert(document, workdir)

        retained = list(cache.rglob("*.pdf"))
        assert len(retained) == 1
        assert retained[0].read_bytes() == b"%PDF-original"
        assert retained[0].stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_authorized_callback_starts_visual_pdf_xlsx_flow():
    async def scenario():
        from plugins.platforms.telegram.adapter import TelegramAdapter

        flow = SimpleNamespace(callback=AsyncMock(return_value=True))
        adapter = object.__new__(TelegramAdapter)
        adapter._pdf_xlsx_flow = flow
        adapter._is_callback_user_authorized = lambda *args, **kwargs: True
        query = SimpleNamespace(
            data="px:start",
            from_user=SimpleNamespace(id=99, first_name="Test"),
            message=SimpleNamespace(chat_id=123, chat=SimpleNamespace(type="private"), message_thread_id=None),
            answer=AsyncMock(),
        )
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())
        flow.callback.assert_awaited_once_with(adapter, query, "px:start", 123, None, "99")

    asyncio.run(scenario())


def test_document_timeout_is_configurable_and_not_fixed_at_sixty_seconds(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTA_PDF_XLSX_DOCUMENT_TIMEOUT_SECONDS", "900")

    flow = PdfXlsxFlow(project_dir=tmp_path)

    assert flow.document_timeout_seconds == 900


def test_backend_timeout_is_reported_as_technical_failure_not_format_rejection(monkeypatch, tmp_path):
    async def scenario():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()), send_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock())
        message = SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            from_user=SimpleNamespace(id=99),
            document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=10),
        )
        monkeypatch.setattr(
            flow,
            "_convert",
            AsyncMock(side_effect=ConversionFailure("VISION_BACKEND_UNAVAILABLE", "PAGE_TIMEOUT", "run-test")),
        )

        await flow.callback(adapter, query, "px:start", 123, None, "99")
        assert await flow.document(adapter, message) is True

        texts = [call.kwargs["text"] for call in adapter._bot.send_message.await_args_list]
        assert texts[-1] == "El servicio de interpretación visual no respondió a tiempo. Volvé a intentar."
        assert all("formato" not in text.lower() for text in texts)

    asyncio.run(scenario())


def test_internal_review_state_is_not_exposed_to_user(monkeypatch, tmp_path):
    async def scenario():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()), send_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock())
        message = SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            from_user=SimpleNamespace(id=99),
            document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=10),
        )
        delivered = tmp_path / "revision.xlsx"
        delivered.write_bytes(b"xlsx")
        monkeypatch.setattr(
            flow,
            "_convert",
            AsyncMock(return_value=(delivered, {"status": "REQUIERE_REVISION", "rows_ok": 4, "review_count": 2})),
        )
        adapter.send_document.return_value = SimpleNamespace(success=True, message_id="77")

        await flow.callback(adapter, query, "px:start", 123, None, "99")
        assert await flow.document(adapter, message) is True

        caption = adapter.send_document.await_args.kwargs["caption"]
        assert "REQUIERE_REVISION" not in caption
        assert "observaciones" not in caption
        assert caption == "Excel generado: 4 movimientos del mes."

    asyncio.run(scenario())


def test_real_pipeline_stages_are_reported_to_telegram(monkeypatch, tmp_path):
    async def scenario():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()), send_document=AsyncMock())
        query = SimpleNamespace(answer=AsyncMock())
        message = SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            from_user=SimpleNamespace(id=99),
            document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=10),
        )
        delivered = tmp_path / "resultado.xlsx"
        delivered.write_bytes(b"xlsx")

        async def fake_convert(document, workdir, progress):
            await progress("rendering", 1, 2)
            await progress("interpreting", 1, 4)
            await progress("validating", 1, 1)
            return delivered, {"status": "VALIDADO", "rows_ok": 2, "review_count": 0}

        monkeypatch.setattr(flow, "_convert", fake_convert)
        adapter.send_document.return_value = SimpleNamespace(success=True, message_id="77")

        await flow.callback(adapter, query, "px:start", 123, None, "99")
        assert await flow.document(adapter, message) is True

        texts = [call.kwargs["text"] for call in adapter._bot.send_message.await_args_list]
        assert "Renderizando páginas…" in texts
        assert "Interpretando el contenido…" in texts
        assert "Validando los datos y generando el Excel…" in texts

    asyncio.run(scenario())
