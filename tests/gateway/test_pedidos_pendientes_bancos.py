"""Pedidos pendientes del menú de bancos (caso real 22/09/2026).

Un usuario tocó «Lote de resúmenes → Excel», no mandó el ZIP, y después eligió
«Resumen bancario → Excel»: el lote se quedaba con cada PDF y respondía
«Esperaba un archivo ZIP…». Ningún pedido vencía ni lo cancelaba la otra opción.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import plugins.platforms.telegram.pdf_xlsx_flow as single_module
from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow
from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow

CHAT, USER = 20, "10"


def _pdf_message():
    document = SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf", file_size=100)
    return SimpleNamespace(chat_id=CHAT, message_thread_id=None, from_user=SimpleNamespace(id=int(USER)), document=document)


def _adapter():
    return SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()))


def test_elegir_resumen_suelto_cancela_un_lote_pendiente(tmp_path):
    async def scenario():
        batch = BatchPdfXlsxFlow(project_dir=tmp_path)
        single = PdfXlsxFlow(project_dir=tmp_path)
        adapter = _adapter()
        query = SimpleNamespace(answer=AsyncMock())
        assert await batch.callback(adapter, query, "bx:start", CHAT, None, USER)
        # Lo que hace el adaptador al tocar «Resumen bancario → Excel».
        assert batch.cancel_pending(CHAT, None, USER) is True
        assert await single.callback(adapter, query, "px:start", CHAT, None, USER)
        # El lote ya no intercepta el PDF: queda para el flujo del PDF suelto.
        assert await batch.document(adapter, _pdf_message()) is False
        assert single.requests
    asyncio.run(scenario())


def test_elegir_lote_cancela_un_pdf_suelto_pendiente(tmp_path):
    async def scenario():
        single = PdfXlsxFlow(project_dir=tmp_path)
        adapter = _adapter()
        assert await single.callback(adapter, SimpleNamespace(answer=AsyncMock()), "px:start", CHAT, None, USER)
        assert single.cancel_pending(CHAT, None, USER) is True
        assert single.cancel_pending(CHAT, None, USER) is False
        assert await single.document(adapter, _pdf_message()) is False
    asyncio.run(scenario())


def test_un_pedido_olvidado_vence_solo(tmp_path, monkeypatch):
    async def scenario():
        batch = BatchPdfXlsxFlow(project_dir=tmp_path)
        adapter = _adapter()
        now = [1000.0]
        monkeypatch.setattr(single_module.time, "monotonic", lambda: now[0])
        assert await batch.callback(adapter, SimpleNamespace(answer=AsyncMock()), "bx:start", CHAT, None, USER)
        adapter._bot.send_message.reset_mock()
        now[0] += single_module.PENDING_REQUEST_TTL_SECONDS + 1
        # Vencido: no responde «Esperaba un ZIP…» ni se queda con el archivo.
        assert await batch.document(adapter, _pdf_message()) is False
        adapter._bot.send_message.assert_not_awaited()
        assert not batch.requests
    asyncio.run(scenario())


def test_el_rechazo_del_lote_queda_en_el_log_y_orienta(tmp_path, caplog):
    async def scenario():
        batch = BatchPdfXlsxFlow(project_dir=tmp_path)
        adapter = _adapter()
        assert await batch.callback(adapter, SimpleNamespace(answer=AsyncMock()), "bx:start", CHAT, None, USER)
        adapter._bot.send_message.reset_mock()
        assert await batch.document(adapter, _pdf_message()) is True
        texto = adapter._bot.send_message.await_args.kwargs["text"]
        assert "Resumen bancario → Excel" in texto
    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert "status=REJECTED reason=not_archive user=10" in caplog.text
