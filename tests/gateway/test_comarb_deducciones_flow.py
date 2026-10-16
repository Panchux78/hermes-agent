"""#133: menú COMARB, pedido por Telegram, rechazos y entrega con acceso revalidado."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram import comarb_deducciones_flow as flow_module
from plugins.platforms.telegram.comarb_deducciones_flow import (
    ComarbDeduccionesEntregas,
    ComarbDeduccionesFlow,
    resumen,
)


def _menu(monkeypatch, page):
    import plugins.platforms.telegram.adapter as adapter_module
    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", lambda text, callback_data: {"text": text, "callback_data": callback_data})
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", lambda rows: rows)
    return TelegramAdapter._menu_panel_title(page), TelegramAdapter._menu_panel_keyboard(page)


def test_comarb_navigation_matches_approved_mockup(monkeypatch):
    title, rows = _menu(monkeypatch, "comarb")
    assert title.startswith("🤝 COMARB")
    assert rows[0] == [{"text": "🔎  Consultar", "callback_data": "om:comarb_consultar"}]
    assert rows[-1][0]["callback_data"] == "om:organismos"
    _, rows = _menu(monkeypatch, "comarb_consultar")
    assert rows[0] == [{"text": "📑  Deducciones de IIBB", "callback_data": "om:comarb_deducciones"}]
    title, rows = _menu(monkeypatch, "comarb_deducciones")
    assert "Sólo lectura" in title
    assert [r[0]["callback_data"] for r in rows[:2]] == ["cd:uno", "cd:cartera"]
    assert rows[-1][0]["callback_data"] == "om:comarb_consultar"


class FakeFlow(ComarbDeduccionesFlow):
    def __init__(self, answers, rows=None):
        super().__init__(query_connection=lambda: None, write_connection=lambda: None)
        self.sql = []
        self.answers = answers
        self.rows = rows or []

    def _write(self, sql):
        self.sql.append(sql)
        return self.answers.pop(0)

    def _query(self, sql):
        self.sql.append(sql)
        return self.rows


class FakeScope:
    def __init__(self, rows):
        self.rows = rows

    def require_actor(self, user):
        return None

    def require_permission(self, user, permission):
        assert permission == "operaciones.ejecutar"

    def search(self, user, term):
        return self.rows

    def by_id(self, user, item):
        return [r for r in self.rows if r["id"] == item]


def _query(data, user=42):
    return SimpleNamespace(data=data, answer=AsyncMock(), from_user=SimpleNamespace(id=user))


def _msg(text, user=42):
    return SimpleNamespace(text=text, chat_id=user, from_user=SimpleNamespace(id=user), message_thread_id=None,
                           reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock(), reply_markup=None)))


ROW = {"id": 7, "nombre": "Empresa Sintética SA", "cuit": "30000000011"}


def _adapter():
    return SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock()))


def test_un_cliente_no_cm_informa_organismo_sin_encolar(monkeypatch):
    async def scenario():
        flow = FakeFlow([[{"estado": "no_cm", "organismos": [{"jurisdiccion": "Ciudad Autónoma de Buenos Aires", "organismo": "AGIP", "servicio": "Ar-Agentes"}]}]])
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([ROW]))
        adapter = _adapter()
        await flow.callback(adapter, _query("cd:uno"), "cd:uno", 42, None, "42")
        await flow.text(adapter, _msg("sintetica"))
        msg = _msg("9/2026")
        assert await flow.text(adapter, msg)
        text = msg.reply_text.await_args.args[0]
        assert "no es de Convenio Multilateral en 09/2026" in text and "AGIP" in text
        assert flow.sql == ["SELECT console.fn_comarb_deducciones_pedir_telegram(42,7,'09/2026')::text;"]
        assert not flow.watchers
    asyncio.run(scenario())


@pytest.mark.parametrize("estado,texto", [("sin_acceso", "acceso actual"), ("clave_sin_verificar", "no está verificada"), ("ocupado", "en curso")])
def test_rechazos_de_la_base_no_consultan(monkeypatch, estado, texto):
    async def scenario():
        flow = FakeFlow([[{"estado": estado}]])
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([ROW]))
        adapter = _adapter()
        await flow.callback(adapter, _query("cd:uno"), "cd:uno", 42, None, "42")
        await flow.text(adapter, _msg("sintetica"))
        msg = _msg("09/2026")
        await flow.text(adapter, msg)
        assert texto in msg.reply_text.await_args.args[0]
        assert not flow.watchers
    asyncio.run(scenario())


def test_sin_resultados_de_busqueda_no_revela_ni_encola(monkeypatch):
    async def scenario():
        flow = FakeFlow([])
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([]))
        adapter = _adapter()
        await flow.callback(adapter, _query("cd:uno"), "cd:uno", 42, None, "42")
        msg = _msg("30-99999999-7")
        await flow.text(adapter, msg)
        assert "con tu acceso actual" in msg.reply_text.await_args.args[0]
        assert flow.sql == []
    asyncio.run(scenario())


def test_cartera_encola_y_avisa_sin_clave(monkeypatch):
    async def scenario():
        flow = FakeFlow([[{"estado": "encolada", "id_lectura": 9, "solicitados": 3, "sin_clave": 1}]])
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([]))
        monkeypatch.setattr(flow, "_watch", AsyncMock())
        adapter = _adapter()
        await flow.callback(adapter, _query("cd:cartera"), "cd:cartera", 42, None, "42")
        msg = _msg("08/2026")
        await flow.text(adapter, msg)
        assert flow.sql == ["SELECT console.fn_comarb_deducciones_cartera_telegram(42,'08/2026')::text;"]
        text = msg.reply_text.await_args.args[0]
        assert "Cartera de Convenio (3)" in text and "1 de Convenio quedan afuera" in text
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_periodo_invalido_no_llama_a_la_base(monkeypatch):
    async def scenario():
        flow = FakeFlow([])
        adapter = _adapter()
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([]))
        await flow.callback(adapter, _query("cd:cartera"), "cd:cartera", 42, None, "42")
        msg = _msg("2026-08")
        await flow.text(adapter, msg)
        assert "MM/AAAA" in msg.reply_text.await_args.args[0] and flow.sql == []
    asyncio.run(scenario())


RESULT = {"estado": "leida", "filas": 5, "posibles_duplicados": 2, "advertencias": ["SIRTAC: consultado antes del día 12 del mes siguiente, fecha orientativa de COMARB; SIFERE puede no haber publicado todo todavía."],
          "estados": {"SIRCREB": {"estado": "con_datos", "filas": 4, "importe": "1500.00"}, "SIRCUPA": {"estado": "sin_datos_verificado"},
                      "SIRTAC": {"estado": "no_verificado", "motivo": "SIFERE no respondió la consulta o la sesión venció."},
                      "SIRPEI": {"estado": "sin_datos_verificado"}, "SIRCAR · Retenciones": {"estado": "con_datos", "filas": 1, "importe": "30.00"},
                      "SIRCAR · Percepciones": {"estado": "sin_datos_verificado"}}}


def test_resumen_separa_regimenes_y_nunca_es_cero_sin_aclarar():
    text = resumen({"nombre_legal": "Empresa Sintética SA", "periodo": "09/2026", "estado": "terminada", "resultado": RESULT})
    assert "Terminada con faltantes." in text
    assert "• SIRCREB: 4 filas · $ 1.500,00" in text
    assert "• SIRTAC: no se pudo verificar. SIFERE no respondió" in text and "día 12" in text
    assert "• SIRPEI: SIFERE no registra deducciones de SIRPEI para 09/2026." in text
    assert "• SIRCAR, retenciones: 1 filas" in text
    assert "posible duplicado" in text


def test_entrega_reclama_antes_de_enviar_y_revalida_acceso(tmp_path):
    async def scenario():
        excel = tmp_path / "estudios/1/30000000011/2026/09/comarb/x-deducciones-iibb-2026-09.xlsx"
        excel.parent.mkdir(parents=True)
        excel.write_bytes(b"PK synthetic")
        import hashlib
        base = {"id_lectura": 9, "id_contribuyente": 7, "telegram_id": 42, "periodo": "09/2026", "estado": "terminada", "motivo": None,
                "resultado": RESULT, "nombre_legal": "Empresa Sintética SA", "raiz": "clientes",
                "ruta": "estudios/1/30000000011/2026/09/comarb/x-deducciones-iibb-2026-09.xlsx",
                "nombre": excel.name, "sha256": hashlib.sha256(excel.read_bytes()).hexdigest()}
        flow = FakeFlow([[base, {**base, "id_contribuyente": 8, "autorizado": False}, {**base, "id_contribuyente": 10}],
                         [True], [True], [False]])
        flow.clients_root = tmp_path
        base["autorizado"] = True
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        await ComarbDeduccionesEntregas(flow).poll(bot)
        assert bot.send_document.await_count == 1  # sólo el autorizado; el tercero ya estaba reclamado
        texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
        assert any("ya no tiene acceso" in t for t in texts)
        assert sum("Empresa Sintética SA" in t for t in texts) == 1
        assert flow.sql[1].startswith("SELECT console.fn_comarb_deducciones_entregada_telegram(9,7)")
    asyncio.run(scenario())
