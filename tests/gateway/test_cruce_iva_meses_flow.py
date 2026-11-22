"""Cruce de varios meses con Libros IVA por Telegram (#152), sin Telegram ni base real."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import cruce_iva_meses_flow as module
from plugins.platforms.telegram.cruce_iva_meses_flow import CruceIvaMesesFlow, State, plan_text, result_text

USER = "12345"
CHAT = 12345


@pytest.fixture(autouse=True)
def plain_markup(monkeypatch):
    monkeypatch.setattr(module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
    monkeypatch.setattr(module, "InlineKeyboardMarkup", lambda rows: rows)


def _flow(tmp_path, respuestas=None):
    clientes = tmp_path / "clientes"
    clientes.mkdir()
    flow = CruceIvaMesesFlow(staging_root=tmp_path / "cruce-iva", clientes_root=clientes)
    llamadas = []

    def call(actor, accion, jid=None, cid=None, datos=None):
        llamadas.append((accion, jid, cid, datos))
        valor = (respuestas or {}).get(accion)
        return valor(datos) if callable(valor) else valor
    flow._call = call
    flow.llamadas = llamadas
    return flow


def _query():
    return SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(),
                           message=SimpleNamespace(edit_text=AsyncMock(), reply_text=AsyncMock()))


def _message(text="", document=None, reply_to=None):
    return SimpleNamespace(chat_id=CHAT, message_thread_id=None, from_user=SimpleNamespace(id=int(USER)), text=text,
                           document=document, reply_to_message=reply_to,
                           reply_text=AsyncMock(return_value=SimpleNamespace(message_id=9, edit_text=AsyncMock())))


PLAN = {"planillas": [{"ruta_relativa": "estudios/1/20000000001/2026/01/bancos/a.xlsx", "banco": "Banco A", "origen": "a.pdf",
                       "periodo": "2026-01"}],
        "meses": [{"periodo": "10/2025", "rol": "soporte", "extractos": 0, "bancos": [], "ventas": True, "compras": True, "bajar": False},
                  {"periodo": "11/2025", "rol": "soporte", "extractos": 0, "bancos": [], "ventas": False, "compras": False, "bajar": True},
                  {"periodo": "12/2025", "rol": "soporte", "extractos": 0, "bancos": [], "ventas": True, "compras": True, "bajar": False},
                  {"periodo": "01/2026", "rol": "movimientos", "extractos": 1, "bancos": ["Banco A"], "ventas": True, "compras": False, "bajar": True},
                  {"periodo": "02/2026", "rol": "sin_extractos", "extractos": 0, "bancos": [], "ventas": False, "compras": False, "bajar": False}],
        "elegible": True, "motivo_acceso": None, "tope_planillas": 60, "activo": None}


def test_el_de_a_uno_no_se_toma():
    flow = CruceIvaMesesFlow()
    assert not flow.handles("cx:start") and not flow.handles("cx:pick:abc")
    assert flow.handles("cx:meses") and flow.handles("cxm:go:abc")


def test_cliente_rango_plan_y_pedido(tmp_path, monkeypatch):
    async def run():
        flow = _flow(tmp_path, {"activo": None, "preparar": PLAN, "pedir": {"id_cruce_meses": 77}})
        q = _query()
        assert await flow.callback(None, q, "cx:meses", CHAT, None, USER)
        assert "Ingresá nombre, CUIT o alias" in q.edit_message_text.await_args.args[0]
        state = flow.states[flow._key(CHAT, None, USER)]
        monkeypatch.setattr(module.CruceIvaFlow, "_search", staticmethod(
            lambda uid, term: [{"id": 9, "nombre": "Cliente A", "slug": "a", "cuit": "20000000001", "study_id": 1}]))
        msg = _message("Cliente A")
        assert await flow.text(None, msg)
        assert state.stage == "range" and state.contributor_id == 9
        for malo in ("enero", "01/2025 a 02/2026"):
            msg = _message(malo)
            assert await flow.text(None, msg)
            assert state.stage == "range"
        msg = _message("01/2026 a 02/2026")
        assert await flow.text(None, msg)
        texto = msg.reply_text.await_args.args[0]
        assert "Se bajan de Portal IVA: 11/2025, 01/2026." in texto
        assert "Sin extractos (no se cruzan): 02/2026" in texto
        flow._watch = lambda *a: None
        q = _query()
        assert await flow.callback(None, q, f"cxm:go:{state.nonce}", CHAT, None, USER)
        accion, _, cid, datos = flow.llamadas[-1]
        assert (accion, cid) == ("pedir", 9)
        assert datos["planillas"] == ["estudios/1/20000000001/2026/01/bancos/a.xlsx"] and datos["pdfs"] == []
        assert flow._key(CHAT, None, USER) not in flow.states
    asyncio.run(run())


def test_pdf_dedup_tope_y_staging_privado(tmp_path):
    async def run():
        flow = _flow(tmp_path)
        state = State("n1", USER, module.time.monotonic(), stage="pdf", contributor_id=9, contributor_name="A",
                      desde="01/2026", hasta="01/2026", plan=PLAN)
        flow.states[flow._key(CHAT, None, USER)] = state
        datos = b"%PDF-1.4 sintetico"
        archivo = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(datos)))
        doc = SimpleNamespace(file_name="extracto.pdf", file_size=len(datos), get_file=AsyncMock(return_value=archivo))
        assert await flow.document(None, _message(document=doc))
        assert await flow.document(None, _message(document=doc))  # el mismo PDF no se suma dos veces
        assert len(state.pdfs) == 1
        destino = flow.staging_root / state.pdfs[0]["staging"]
        assert destino.read_bytes() == datos and (destino.stat().st_mode & 0o777) == 0o600
        malo = SimpleNamespace(file_name="extracto.xlsx", file_size=10, get_file=AsyncMock())
        assert await flow.document(None, _message(document=malo))
        assert len(state.pdfs) == 1
        flow._drop(flow._key(CHAT, None, USER))
        assert not destino.exists()  # cancelar borra los PDF que no llegaron a pedirse
    asyncio.run(run())


def test_entrega_una_sola_vez_y_captcha(tmp_path, monkeypatch):
    async def run():
        estado = {"id_cruce_meses": 5, "contribuyente": "Cliente A", "desde": "01/2026", "hasta": "03/2026", "estado": "sin_libros",
                  "error": "No hay Libros IVA de ningún mes.", "progreso": "x", "reintentable": True, "resultado": {}}
        reclamos = iter([estado, None])
        flow = _flow(tmp_path, {"reclamar": lambda d: next(reclamos), "captcha_imagen": {"imagen": "aGVsbG8="},
                                "captcha": lambda d: {"ok": d["solucion"] == "AB12"}})
        consultas = []

        def query(sql):
            consultas.append(sql)
            if "fn_cruce_iva_meses_entregas()" in sql:
                return [{"id_cruce_meses": 5, "telegram_id": CHAT}]
            if "captchas_telegram" in sql:
                return [{"id_cruce_meses": 5, "telegram_id": CHAT, "nonce": "a" * 32}]
            return [True]
        flow._query = query
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=31)),
                              send_photo=AsyncMock(return_value=SimpleNamespace(message_id=40)))
        adapter = SimpleNamespace(_bot=bot)
        monkeypatch.setattr("telegram.ForceReply", lambda **kw: None, raising=False)
        await flow.poll(adapter)
        await flow.poll(adapter)
        assert bot.send_photo.await_count == 1  # el mismo CAPTCHA no se manda dos veces
        assert bot.send_message.await_count == 1  # la entrega se reclama una sola vez
        assert "No hay Libros IVA" in bot.send_message.await_args.kwargs["text"]
        assert any("fn_cruce_iva_meses_entrega(12345,5,'enviada',31)" in c for c in consultas)
        monkeypatch.setattr("plugins.platforms.telegram.panel_captcha_bridge.teclado_menu", lambda: None)
        respuesta = _message("AB12", reply_to=SimpleNamespace(message_id=40))
        assert await flow.text(None, respuesta)
        assert "Recibido" in respuesta.reply_text.await_args.args[0]
    asyncio.run(run())


def test_textos_del_resultado():
    fila = {"contribuyente": "Cliente A", "desde": "01/2026", "hasta": "03/2026", "estado": "parcial",
            "resultado": {"movimientos": 10, "deudores": 4, "proveedores": 3, "en_blanco": 3,
                          "pendiente": "02/2026: ARCA informa que no hay libro presentado."},
            "meses": [{"ventas": {"estado": "bajado"}, "compras": {"estado": "ya_estaba"}}]}
    texto = result_text(fila)
    assert "10 movimientos · 4 deudores · 3 proveedores · 3 en blanco." in texto
    assert "Sin cruzar completo: 02/2026: ARCA informa" in texto
    assert "Se bajaron de Portal IVA 1 mes" in texto
    estado = State("n", USER, 0, contributor_name="Cliente A", desde="01/2026", hasta="02/2026",
                   plan={**PLAN, "elegible": False, "motivo_acceso": "no tiene un acceso ARCA verificado y una representación única"})
    assert "no se pueden bajar" in plan_text(estado)
