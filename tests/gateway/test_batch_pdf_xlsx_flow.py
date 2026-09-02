import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow


def test_start_requests_zip_and_inspection_offers_process_cancel(monkeypatch, tmp_path):
    async def scenario():
        import plugins.platforms.telegram.batch_pdf_xlsx_flow as module
        monkeypatch.setattr(module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
        monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: rows)
        flow = BatchPdfXlsxFlow(project_dir=tmp_path)
        flow.input_cache = tmp_path / "cache"
        flow.batch_root = tmp_path / "batches"
        query = SimpleNamespace(answer=AsyncMock())
        prompt = SimpleNamespace(edit_text=AsyncMock())
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock(side_effect=[None, prompt])))
        telegram_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=lambda custom_path: Path(custom_path).write_bytes(b"ZIP")))
        document = SimpleNamespace(file_name="lote.zip", file_size=100, get_file=AsyncMock(return_value=telegram_file))
        message = SimpleNamespace(chat_id=20, message_thread_id=None, from_user=SimpleNamespace(id=10), document=document)
        result = {"batch_id": "abc", "digest": "d" * 64, "groups": [{
            "contributor_name": "Empresa", "entity_name": "Banco", "pdf_count": 12,
            "currencies": ["ARS"], "periods": ["2025/06", "2025/07", "2025/08", "2025/09", "2025/10", "2025/11", "2025/12", "2026/01", "2026/02", "2026/03", "2026/04", "2026/05"],
            "period_start": "2025/06", "period_end": "2026/05", "distinct_period_count": 12,
            "is_contiguous": True, "is_twelve_month_period": True,
            "missing_periods": [], "repeated_periods": [],
        }], "pdf_count": 12, "problems": [], "status": "AWAITING_CONFIRMATION", "warnings": []}
        monkeypatch.setattr(flow, "_run", AsyncMock(return_value=result))
        monkeypatch.setattr(flow, "_command", lambda *args: list(args))

        assert await flow.callback(adapter, query, "bx:start", 20, None, "10")
        assert await flow.document(adapter, message)

        query.answer.assert_awaited_once_with("Lote de resúmenes bancarios → Excel")
        prompt.edit_text.assert_awaited_once()
        markup = prompt.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [button["callback_data"] for row in markup for button in row]
        assert callbacks == ["bx:p:abc", "bx:c:abc"]
        text = prompt.edit_text.await_args.args[0]
        assert "se generará 1 XLSX" in text
        assert "2025/06 a 2026/05" in text
        assert "12 meses consecutivos" in text
        assert not any(flow.input_cache.glob("*"))

    asyncio.run(scenario())


def test_blocked_inspection_lists_problem_and_preserved_archive(monkeypatch, tmp_path):
    async def scenario():
        flow = BatchPdfXlsxFlow(project_dir=tmp_path)
        flow.input_cache = tmp_path / "cache"
        prompt = SimpleNamespace(edit_text=AsyncMock())
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock(side_effect=[None, prompt])))
        query = SimpleNamespace(answer=AsyncMock())
        telegram_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=lambda custom_path: Path(custom_path).write_bytes(b"ZIP")))
        message = SimpleNamespace(chat_id=20, message_thread_id=None, from_user=SimpleNamespace(id=10), document=SimpleNamespace(file_name="lote.zip", file_size=100, get_file=AsyncMock(return_value=telegram_file)))
        monkeypatch.setattr(flow, "_run", AsyncMock(return_value={"status": "BLOCKED", "problems": [{"member_name": "enero.pdf", "reason": "contribuyente_no_identificado"}]}))
        monkeypatch.setattr(flow, "_command", lambda *args: list(args))
        await flow.callback(adapter, query, "bx:start", 20, None, "10")
        await flow.document(adapter, message)
        text = prompt.edit_text.await_args.args[0]
        assert "enero.pdf" in text
        assert "original quedó conservado" in text

    asyncio.run(scenario())


def test_preview_reports_missing_and_repeated_periods(monkeypatch, tmp_path):
    async def scenario():
        import plugins.platforms.telegram.batch_pdf_xlsx_flow as module
        monkeypatch.setattr(module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
        monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: rows)
        flow = BatchPdfXlsxFlow(project_dir=tmp_path)
        flow.input_cache = tmp_path / "cache"
        flow.batch_root = tmp_path / "batches"
        prompt = SimpleNamespace(edit_text=AsyncMock())
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock(side_effect=[None, prompt])))
        query = SimpleNamespace(answer=AsyncMock())
        telegram_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=lambda custom_path: Path(custom_path).write_bytes(b"ZIP")))
        message = SimpleNamespace(chat_id=20, message_thread_id=None, from_user=SimpleNamespace(id=10), document=SimpleNamespace(file_name="lote.zip", file_size=100, get_file=AsyncMock(return_value=telegram_file)))
        result = {"batch_id": "abc", "digest": "d" * 64, "groups": [{
            "contributor_name": "Empresa", "entity_name": "Banco", "pdf_count": 3,
            "currencies": ["ARS"], "periods": ["2025/11", "2026/01"],
            "period_start": "2025/11", "period_end": "2026/01", "distinct_period_count": 2,
            "is_contiguous": False, "is_twelve_month_period": False,
            "missing_periods": ["2025/12"], "repeated_periods": ["2026/01"],
        }], "pdf_count": 3, "problems": [], "status": "AWAITING_CONFIRMATION", "warnings": []}
        monkeypatch.setattr(flow, "_run", AsyncMock(return_value=result))
        monkeypatch.setattr(flow, "_command", lambda *args: list(args))

        await flow.callback(adapter, query, "bx:start", 20, None, "10")
        await flow.document(adapter, message)

        text = prompt.edit_text.await_args.args[0]
        assert "faltan los períodos 2025/12" in text
        assert "más de un PDF para 2026/01" in text

    asyncio.run(scenario())


def test_process_delivers_each_group_and_marks_it(monkeypatch, tmp_path):
    async def scenario():
        flow = BatchPdfXlsxFlow(project_dir=tmp_path)
        output = tmp_path / "grupo.xlsx"
        output.write_bytes(b"xlsx")
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
        flow._status = AsyncMock(return_value={"digest": "a" * 64})
        flow._command = lambda *args: list(args)
        flow._run = AsyncMock(side_effect=[{"status": "COMPLETED", "outputs": [{"path": str(output), "sha256": "b" * 64, "document_count": 2, "row_count": 9, "delivered": False}]}, {"status": "OK"}])

        assert await flow.callback(adapter, query, "bx:p:batch-1", 20, None, "10")

        adapter.send_document.assert_awaited_once()
        assert flow._run.await_count == 2
        assert "Se entregaron 1" in query.edit_message_text.await_args.args[0]

    asyncio.run(scenario())
