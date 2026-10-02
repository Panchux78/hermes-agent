"""Cruce con Libros IVA por Telegram (Ágora #122), sin Telegram ni base real."""
import asyncio
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from plugins.platforms.telegram import cruce_iva_flow as module
from plugins.platforms.telegram.cruce_iva_flow import CruceIvaFlow, State
from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow


USER = "12345"
CHAT = 55


@pytest.fixture(autouse=True)
def plain_markup(monkeypatch):
    monkeypatch.setattr(module, "InlineKeyboardButton",
                        lambda text, callback_data: {"text": text, "callback_data": callback_data})
    monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: rows)


def _flow(tmp_path):
    clientes = tmp_path / "clientes"
    clientes.mkdir()
    return CruceIvaFlow(staging_root=tmp_path / "cruce-iva", clientes_root=clientes)


def _query(message=None):
    return SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(),
                           message=message or SimpleNamespace(edit_text=AsyncMock()))


def _state(flow, stage="source", **kwargs):
    state = State("nonce", USER, module.time.monotonic(), stage=stage, contributor_id=9,
                  contributor_name="Ejemplo SA", **kwargs)
    flow.states[flow._key(CHAT, None, USER)] = state
    return state


PLANILLAS = [
    {"ruta": "ejemplo/output/galicia/2026/08/resumen/agosto.xlsx", "banco": "Banco Galicia",
     "origen": "Resumen cuenta corriente agosto 2026 numero largo.pdf", "periodo": "2026-08"},
    {"ruta": "ejemplo/output/icbc/2026/07/resumen/julio.xlsx", "banco": "ICBC",
     "origen": "julio.pdf", "periodo": "2026-07"},
]


def test_start_asks_for_client_and_source_offers_two_options(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        query = _query()
        assert await flow.callback(None, query, "cx:start", CHAT, None, USER)
        text = query.edit_message_text.await_args.args[0]
        assert text == "Cruce con Libros IVA\nIngresá nombre, CUIT o alias del contribuyente."
        state = flow.states[flow._key(CHAT, None, USER)]
        rows = [{"id": 9, "nombre": "Ejemplo SA", "slug": "ejemplo", "cuit": "30712345678", "study_id": 1}]
        message = SimpleNamespace(chat_id=CHAT, from_user=SimpleNamespace(id=int(USER)), text="Ejemplo",
                                  reply_text=AsyncMock())
        with patch.object(CruceIvaFlow, "_query", return_value=rows) as db:
            assert await flow.text(None, message)
        assert "fn_constancia_telegram_buscar(12345," in db.call_args.args[0]
        assert state.stage == "source" and state.contributor_id == 9
        reply = message.reply_text.await_args
        assert reply.args[0] == "Cruce con Libros IVA · Ejemplo SA\n¿Con qué Excel querés cruzar?"
        labels = [row[0]["text"] for row in reply.kwargs["reply_markup"]]
        assert labels == ["📂  Elegir Excel ya generados", "📤  Subir un Excel", "✕  Cancelar"]
    asyncio.run(run())


def test_multiple_clients_use_person_buttons(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        _state(flow, stage="search")
        rows = [{"id": 1, "nombre": "A", "slug": "a", "cuit": "20123456789", "study_id": 1},
                {"id": 2, "nombre": "B", "slug": "b", "cuit": "20987654321", "study_id": 1}]
        message = SimpleNamespace(chat_id=CHAT, from_user=SimpleNamespace(id=int(USER)), text="x",
                                  reply_text=AsyncMock())
        from plugins.platforms.telegram import contributor_selector
        with patch.object(CruceIvaFlow, "_query", return_value=rows), \
             patch.object(contributor_selector, "InlineKeyboardMarkup", lambda rows: rows), \
             patch.object(contributor_selector, "InlineKeyboardButton",
                          lambda text, callback_data: {"text": text, "callback_data": callback_data}):
            assert await flow.text(None, message)
        keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        assert keyboard[0][0]["text"].startswith("👤  A")
        assert keyboard[1][0]["callback_data"] == "cx:select:nonce:2"
    asyncio.run(run())


def test_pick_lists_planillas_and_toggles_multiple_selection(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        state = _state(flow)
        query = _query()
        with patch.object(CruceIvaFlow, "_query", return_value=PLANILLAS) as db:
            assert await flow.callback(None, query, "cx:pick:nonce", CHAT, None, USER)
        assert "fn_cruce_iva_telegram_planillas(12345,9)" in db.call_args.args[0]
        keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
        assert keyboard[0][0]["text"] == "⬜  Banco Galicia · 08/2026 · Resumen cuenta corrient…"
        assert keyboard[1][0]["text"] == "⬜  ICBC · 07/2026 · julio"
        assert keyboard[-2][0] == {"text": "🔀  Cruzar seleccionados", "callback_data": "cx:go:nonce"}
        assert keyboard[-1][0]["text"] == "✕  Cancelar"

        await flow.callback(None, query, "cx:t:nonce:0", CHAT, None, USER)
        await flow.callback(None, query, "cx:t:nonce:1", CHAT, None, USER)
        await flow.callback(None, query, "cx:t:nonce:1", CHAT, None, USER)
        assert state.selected == {0}
        keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
        assert keyboard[0][0]["text"].startswith("✅  Banco Galicia")
        assert keyboard[1][0]["text"].startswith("⬜  ICBC")
        assert "(1 elegido)" in query.edit_message_text.await_args.args[0]

        with patch.object(CruceIvaFlow, "_begin", return_value=77) as begin, \
             patch.object(CruceIvaFlow, "_poll", AsyncMock()):
            await flow.callback(None, query, "cx:go:nonce", CHAT, None, USER)
            await asyncio.sleep(0)
        begin.assert_called_once_with(12345, 9, planillas=[PLANILLAS[0]["ruta"]])
        assert state.stage == "running" and state.cruce_id == 77
        query.message.edit_text.assert_awaited_with("Cruzando con Libros IVA…", reply_markup=None)
    asyncio.run(run())


def test_go_without_selection_is_refused(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        _state(flow, stage="pick", planillas=list(PLANILLAS))
        query = _query()
        with patch.object(CruceIvaFlow, "_begin") as begin:
            assert await flow.callback(None, query, "cx:go:nonce", CHAT, None, USER)
        begin.assert_not_called()
        query.answer.assert_awaited_with("Elegí al menos un Excel.")
    asyncio.run(run())


def test_no_planillas_offers_upload(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        _state(flow)
        query = _query()
        with patch.object(CruceIvaFlow, "_query", return_value=[]):
            await flow.callback(None, query, "cx:pick:nonce", CHAT, None, USER)
        text = query.edit_message_text.await_args.args[0]
        assert "Todavía no hay Excel de resúmenes bancarios de este cliente. Podés subir uno." in text
        keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
        assert keyboard[0][0]["callback_data"] == "cx:up:nonce"
    asyncio.run(run())


def _document_message(name, size, data=b"PK\x03\x04xlsx"):
    telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    document = SimpleNamespace(file_name=name, file_size=size, get_file=AsyncMock(return_value=telegram_file))
    progress = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(chat_id=CHAT, message_thread_id=None, from_user=SimpleNamespace(id=int(USER)),
                           document=document, reply_text=AsyncMock(return_value=progress))


@pytest.mark.parametrize(("name", "size", "expected"), [
    ("planilla.xls", 100, "Esperaba un Excel con extensión .xlsx. Mandalo de nuevo como documento."),
    ("planilla.pdf", 100, "Esperaba un Excel con extensión .xlsx. Mandalo de nuevo como documento."),
    ("planilla.xlsx", 15 * 1024 * 1024 + 1, "El Excel supera el límite de 15 MB o Telegram no informó su tamaño."),
    ("planilla.xlsx", 0, "El Excel supera el límite de 15 MB o Telegram no informó su tamaño."),
])
def test_upload_rejects_extension_and_size(tmp_path, name, size, expected):
    async def run():
        flow = _flow(tmp_path)
        state = _state(flow, stage="upload")
        message = _document_message(name, size)
        with patch.object(CruceIvaFlow, "_begin") as begin:
            assert await flow.document(None, message)
        begin.assert_not_called()
        assert message.reply_text.await_args.args[0] == expected
        assert state.stage == "upload"
        assert not (tmp_path / "cruce-iva").exists()
    asyncio.run(run())


def test_upload_writes_private_staging_and_enqueues_original_name(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        state = _state(flow, stage="upload")
        message = _document_message("Movimientos Agosto.XLSX", 1000)
        with patch.object(CruceIvaFlow, "_begin", return_value=81) as begin, \
             patch.object(CruceIvaFlow, "_poll", AsyncMock()):
            assert await flow.document(None, message)
            await asyncio.sleep(0)
        kwargs = begin.call_args.kwargs
        assert kwargs["archivo_nombre"] == "Movimientos Agosto.XLSX"
        assert module.STAGING_PATTERN.fullmatch(kwargs["archivo_staging"])
        assert kwargs["archivo_staging"].startswith("telegram/12345/")
        staged = tmp_path / "cruce-iva" / kwargs["archivo_staging"]
        assert staged.read_bytes() == b"PK\x03\x04xlsx"
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        assert stat.S_IMODE(staged.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "cruce-iva").stat().st_mode) == 0o700
        assert state.cruce_id == 81
    asyncio.run(run())


def test_begin_sql_binds_actor_and_validates_origin():
    with patch.object(CruceIvaFlow, "_query", return_value=[{"id_cruce": 5}]) as db:
        assert CruceIvaFlow._begin(12345, 9, planillas=["a/output/b/2026/08/x.xlsx"]) == 5
    sql = db.call_args.args[0]
    assert "fn_cruce_iva_telegram_iniciar(12345,9,ARRAY[convert_from(" in sql
    assert "NULL::text,NULL::text" in sql
    staging = "telegram/12345/" + "a" * 32 + ".xlsx"
    with patch.object(CruceIvaFlow, "_query", return_value=[{"id_cruce": 6}]) as db:
        assert CruceIvaFlow._begin(12345, 9, archivo_nombre="x.xlsx", archivo_staging=staging) == 6
    assert "'{}'::text[]" in db.call_args.args[0]
    with pytest.raises(ValueError):
        CruceIvaFlow._begin(12345, 9, archivo_nombre="x.xlsx", archivo_staging="../x.xlsx")
    with pytest.raises(ValueError):
        CruceIvaFlow._begin(12345, 9, planillas=["a.xlsx"], archivo_nombre="x.xlsx", archivo_staging=staging)


def _running(flow, **kwargs):
    state = _state(flow, stage="running", cruce_id=77, **kwargs)
    state.progress_message = SimpleNamespace(edit_text=AsyncMock())
    return state


def test_finished_cruce_delivers_excel_with_summary_and_missing_books(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        output = tmp_path / "clientes" / "ejemplo" / "output" / "cruce-iva" / "cruce.xlsx"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"xlsx")
        state = _running(flow)
        status = {"estado": "terminado", "salida_relativa": "ejemplo/output/cruce-iva/cruce.xlsx",
                  "resultado": {"movimientos": 120, "con_cuit": 80, "deudores": 12, "proveedores": 30,
                                "en_blanco": 5, "libros_faltantes": ["2026-07", "2026-08"], "archivos": 2}}
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)),
                                  _bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(CruceIvaFlow, "_status", return_value=status):
            await flow._poll(adapter, state, flow._key(CHAT, None, USER), CHAT, None)
        sent = adapter.send_document.await_args.kwargs
        assert sent["file_path"] == str(tmp_path / "clientes" / "ejemplo/output/cruce-iva/cruce.xlsx")
        assert sent["file_name"] == "cruce.xlsx"
        assert sent["caption"] == "120 movimientos · 12 deudores · 30 proveedores · 5 en blanco"
        final = state.progress_message.edit_text.await_args.args[0]
        assert final == (
            "Cruce con Libros IVA terminado: 120 movimientos · 12 deudores · 30 proveedores · 5 en blanco.\n"
            "Faltan los Libros IVA de 07/2026, 08/2026: bajalos con ARCA › Consultar › Lote de Libros IVA."
        )
        assert flow.states == {}
    asyncio.run(run())


def test_output_outside_root_or_symlink_is_not_sent(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        outside = tmp_path / "secreto.xlsx"
        outside.write_bytes(b"x")
        (tmp_path / "clientes" / "link.xlsx").symlink_to(outside)
        adapter = SimpleNamespace(send_document=AsyncMock(), _bot=SimpleNamespace(send_message=AsyncMock()))
        for relative in ("link.xlsx", "../secreto.xlsx", "/etc/passwd"):
            state = _running(flow)
            status = {"estado": "terminado", "salida_relativa": relative, "resultado": {}}
            with patch.object(CruceIvaFlow, "_status", return_value=status):
                await flow._poll(adapter, state, flow._key(CHAT, None, USER), CHAT, None)
            assert state.progress_message.edit_text.await_args.args[0] == (
                "El cruce terminó, pero no encontré el Excel resultante. Volvé a intentarlo en unos minutos.")
        adapter.send_document.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize(("status", "expected"), [
    ({"estado": "formato_invalido", "error": "El Excel no tiene las columnas de un resumen bancario.",
      "resultado": {"archivo": "x.xlsx", "problemas": ["Falta la columna Fecha.", "Falta la columna Importe."]}},
     "El Excel no tiene las columnas de un resumen bancario.\n• Falta la columna Fecha.\n• Falta la columna Importe."),
    ({"estado": "sin_libros", "error": "No hay Libros IVA de 07/2026 para Ejemplo SA.", "resultado": None},
     "No hay Libros IVA de 07/2026 para Ejemplo SA."),
    ({"estado": "fallido", "error": "No se pudo leer la planilla.", "resultado": None},
     "No se pudo leer la planilla."),
])
def test_terminal_errors_are_shown_to_the_user(tmp_path, status, expected):
    async def run():
        flow = _flow(tmp_path)
        state = _running(flow)
        adapter = SimpleNamespace(send_document=AsyncMock(), _bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(CruceIvaFlow, "_status", return_value=status):
            await flow._poll(adapter, state, flow._key(CHAT, None, USER), CHAT, None)
        assert state.progress_message.edit_text.await_args.args[0] == expected
        adapter.send_document.assert_not_awaited()
    asyncio.run(run())


def test_poll_waits_and_reports_timeout(tmp_path, monkeypatch):
    async def run():
        flow = _flow(tmp_path)
        flow.POLL_SECONDS = 0
        flow.POLL_TIMEOUT_SECONDS = 0
        state = _running(flow)
        adapter = SimpleNamespace(send_document=AsyncMock(), _bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(CruceIvaFlow, "_status", return_value={"estado": "procesando"}):
            await flow._poll(adapter, state, flow._key(CHAT, None, USER), CHAT, None)
        assert "tardando más de 10 minutos" in state.progress_message.edit_text.await_args.args[0]
        assert flow.states == {}
    asyncio.run(run())


def test_begin_failure_answers_and_removes_staging(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        _state(flow, stage="upload")
        message = _document_message("planilla.xlsx", 100)
        with patch.object(CruceIvaFlow, "_begin", side_effect=RuntimeError("db")):
            assert await flow.document(None, message)
        progress = message.reply_text.return_value
        assert progress.edit_text.await_args.args[0].startswith("No pude iniciar el cruce.")
        assert list((tmp_path / "cruce-iva" / "telegram" / USER).iterdir()) == []
        assert flow.states == {}
    asyncio.run(run())


def test_offer_after_conversion_starts_that_planilla(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        ruta = PLANILLAS[0]["ruta"]
        keyboard = flow.register_offer(user_id=USER, chat_id=CHAT, thread_id=None,
                                       contributor_id=9, ruta_relativa=ruta)
        button = keyboard[0][0]
        assert button["text"] == "🔀  Cruzar con Libros IVA"
        query = _query()
        with patch.object(CruceIvaFlow, "_query", return_value=PLANILLAS), \
             patch.object(CruceIvaFlow, "_begin", return_value=90) as begin, \
             patch.object(CruceIvaFlow, "_poll", AsyncMock()):
            assert await flow.callback(None, query, button["callback_data"], CHAT, None, USER)
            await asyncio.sleep(0)
        begin.assert_called_once_with(12345, 9, planillas=[ruta])
        query.message.edit_text.assert_awaited_with("Cruzando con Libros IVA…", reply_markup=None)
        other = _query()
        assert await flow.callback(None, other, button["callback_data"], CHAT, None, USER)
        other.answer.assert_awaited_with("Esta opción venció. Iniciá el cruce desde Bancos › Cruce con Libros IVA.")
    asyncio.run(run())


def test_offer_with_unlisted_planilla_opens_normal_flow_for_that_client(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        keyboard = flow.register_offer(user_id=USER, chat_id=CHAT, thread_id=None,
                                       contributor_id=9, ruta_relativa="otro/output/x/2026/08/y.xlsx")
        query = _query()
        with patch.object(CruceIvaFlow, "_query", return_value=PLANILLAS), \
             patch.object(CruceIvaFlow, "_begin") as begin:
            await flow.callback(None, query, keyboard[0][0]["callback_data"], CHAT, None, USER)
        begin.assert_not_called()
        assert query.edit_message_text.await_args.args[0] == (
            "Ese Excel todavía no está disponible para cruzar.\n"
            "Cruce con Libros IVA · este cliente\n¿Con qué Excel querés cruzar?")
        state = flow.states[flow._key(CHAT, None, USER)]
        assert state.stage == "source" and state.contributor_id == 9
    asyncio.run(run())


def test_offer_is_bound_to_the_user_who_converted(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        keyboard = flow.register_offer(user_id=USER, chat_id=CHAT, thread_id=None,
                                       contributor_id=9, ruta_relativa=PLANILLAS[0]["ruta"])
        query = _query()
        with patch.object(CruceIvaFlow, "_query") as db:
            await flow.callback(None, query, keyboard[0][0]["callback_data"], CHAT, None, "999")
        db.assert_not_called()
    asyncio.run(run())


def test_resumen_bancario_offers_cruce_after_delivery(monkeypatch, tmp_path):
    async def run():
        flow = PdfXlsxFlow(project_dir=tmp_path)
        cruce = _flow(tmp_path)
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()),
                                  send_document=AsyncMock(return_value=SimpleNamespace(success=True)),
                                  _cruce_iva_flow=cruce)
        message = SimpleNamespace(chat_id=CHAT, message_thread_id=None, from_user=SimpleNamespace(id=int(USER)),
                                  document=SimpleNamespace(file_name="resumen.pdf", mime_type="application/pdf",
                                                           file_size=10))
        delivered = tmp_path / "resumen.xlsx"
        delivered.write_bytes(b"xlsx")
        result = {"rows_ok": 6, "id_contribuyente": 9, "output_relative_to_clientes": PLANILLAS[0]["ruta"]}
        monkeypatch.setattr(flow, "_convert", AsyncMock(return_value=(delivered, result)))
        assert await flow.callback(adapter, SimpleNamespace(answer=AsyncMock()), "px:start", CHAT, None, USER)
        assert await flow.document(adapter, message)
        final = adapter._bot.send_message.await_args.kwargs
        assert final["text"] == "Excel enviado."
        button = final["reply_markup"][0][0]
        assert button["text"] == "🔀  Cruzar con Libros IVA"
        offer = cruce.offers[button["callback_data"].removeprefix("cx:of:")]
        assert (offer.contributor_id, offer.ruta_relativa, offer.user_id) == (9, PLANILLAS[0]["ruta"], USER)

        # Sin contribuyente resuelto no se ofrece el cruce.
        monkeypatch.setattr(flow, "_convert", AsyncMock(return_value=(delivered, {"rows_ok": 6})))
        assert await flow.callback(adapter, SimpleNamespace(answer=AsyncMock()), "px:start", CHAT, None, USER)
        assert await flow.document(adapter, message)
        assert "reply_markup" not in adapter._bot.send_message.await_args.kwargs
    asyncio.run(run())


def test_cancel_pending_keeps_running_cruce(tmp_path):
    flow = _flow(tmp_path)
    _state(flow, stage="upload")
    assert flow.cancel_pending(CHAT, None, USER) is True
    _running(flow)
    assert flow.cancel_pending(CHAT, None, USER) is False


def test_bancos_menu_lists_cruce_below_the_bank_statements(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.telegram.menu_buttons import MENU_ALIGNMENT_PADDING, aligned_menu_label

    monkeypatch.setattr(adapter_module, "InlineKeyboardButton",
                        lambda text, callback_data: {"text": text, "callback_data": callback_data})
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)
    rows = TelegramAdapter._menu_panel_keyboard("bancos")
    assert [row[0]["callback_data"] for row in rows[:3]] == ["px:start", "bx:start", "cx:start"]
    assert rows[2][0]["text"] == aligned_menu_label("bancos", "🔀", "Cruce con Libros IVA")
    assert ("bancos", "Cruce con Libros IVA") in MENU_ALIGNMENT_PADDING
