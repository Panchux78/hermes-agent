"""#133: menú COMARB, pedido por Telegram, rechazos y entrega con acceso revalidado."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig
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
    # #191: entre «Un cliente» y «Cartera», elegir de una lista.
    assert [r[0]["callback_data"] for r in rows[:3]] == ["cd:uno", "cd:lote", "cd:cartera"]
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


def test_local_agip_no_inventa_consultas_sifere_ni_ceros_desconocidos():
    states={r:{'estado':'no_aplica'} for r in flow_module.REGIMENES}
    states.update({'AGIP · Retenciones':{'estado':'con_datos','filas':2,'importe':'10.25'},
        'AGIP · Percepciones':{'estado':'no_verificado','motivo':'No se confirmó el filtro.'},
        'AGIP · Retenciones bancarias':{'estado':'sin_datos_verificado'},
        'AGIP · Percepciones aduaneras':{'estado':'sin_datos_verificado'}})
    result=resumen({'nombre_legal':'Empresa Sintética SA','periodo':'08/2026','estado':'terminada',
        'resultado':{'estados':states}})
    assert '\nTerminada con faltantes.\n' in result and '• AGIP · Percepciones: no se pudo consultar.' in result
    assert '2 filas · $ 10,25' in result
    assert 'SIFERE no registra' not in result
    assert 'No se confirmó el filtro' in result
    assert '$ 0,00' not in result


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
        assert not flow.sql
        from tests.gateway.period_helpers import confirm_period
        await confirm_period(flow,adapter,42,'42')
        text = msg.reply_text.await_args.args[0]
        assert "no es de Convenio Multilateral en 09/2026" in text and "AGIP" in text
        assert flow.sql == ["SELECT console.fn_comarb_deducciones_pedir_telegram(42,7,'09/2026')::text;"]
        assert not flow.watchers
    asyncio.run(scenario())


@pytest.mark.parametrize("estado,texto", [("sin_acceso", "acceso actual"), ("clave_sin_verificar", "No hay una clave cargada"), ("solo_miba", "sólo miBA"), ("ocupado", "en curso"), ("regimen_no_verificado","régimen de Ingresos Brutos")])
def test_rechazos_de_la_base_no_consultan(monkeypatch, estado, texto):
    async def scenario():
        flow = FakeFlow([[{"estado": estado}]])
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([ROW]))
        adapter = _adapter()
        await flow.callback(adapter, _query("cd:uno"), "cd:uno", 42, None, "42")
        await flow.text(adapter, _msg("sintetica"))
        msg = _msg("09/2026")
        await flow.text(adapter, msg)
        from tests.gateway.period_helpers import confirm_period
        await confirm_period(flow,adapter,42,'42')
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
        flow = FakeFlow([[{"estado": "previa", "candidatos": 3, "sin_clave": 1}],
                         [{"estado": "encolada", "id_lectura": 9, "solicitados": 3, "sin_clave": 1}]])
        monkeypatch.setattr(flow, "_scope", lambda: FakeScope([]))
        monkeypatch.setattr(flow, "_watch", AsyncMock())
        adapter = _adapter()
        await flow.callback(adapter, _query("cd:cartera"), "cd:cartera", 42, None, "42")
        msg = _msg("08/2026")
        await flow.text(adapter, msg)
        from tests.gateway.period_helpers import confirm_period
        await confirm_period(flow,adapter,42,'42')
        # #191: antes de encolar, la cantidad; nada se encola sin «Sí, consultar».
        assert flow.sql == ["SELECT console.fn_comarb_deducciones_cartera_previa_telegram(42,'08/2026','deducciones')::text;"]
        pregunta = msg.reply_text.await_args.args[0]
        assert "Voy a consultar 3 clientes" in pregunta and "1 quedan afuera" in pregunta
        nonce = flow.states[flow._key(42, None, "42")].nonce
        await flow.callback(adapter, _query(f"cd:cc:{nonce}"), f"cd:cc:{nonce}", 42, None, "42")
        assert flow.sql[-1] == "SELECT console.fn_comarb_deducciones_cartera_telegram(42,'08/2026')::text;"
        text = adapter._bot.send_message.await_args.kwargs["text"]
        assert "Cartera de IIBB (3)" in text and "1 quedan afuera" in text
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


def test_sircupa_neutro_y_faltantes_identificados():
    from copy import deepcopy
    result = deepcopy(RESULT)
    for regime in result['estados']:
        result['estados'][regime] = {'estado':'sin_datos_verificado'}
    result['estados']['SIRCUPA'] = {'estado':'no_verificado','motivo':'No se observaron CVU en la consulta conjunta. Eso no demuestra ausencia de recaudaciones SIRCUPA.'}
    row = {'nombre_legal':'Empresa Sintética SA','periodo':'09/2026','estado':'terminada','resultado':result}
    text = resumen(row)
    # #191: el SIRCUPA sin CVU no es un faltante y dice si hay que hacer algo.
    assert '\nTerminada.\n' in text
    # #191: el mismo texto del pedido 10 que el panel y el Excel.
    assert '• SIRCUPA: SIFERE no muestra cuentas virtuales (CVU) del cliente, así que no se trajeron recaudaciones de SIRCUPA.' in text
    assert 'no hay que hacer nada' in text
    result['estados']['SIRCAR · Percepciones'] = {'estado':'no_verificado','motivo':'Sesión vencida'}
    assert '\nTerminada con faltantes.\n' in resumen(row) and '• SIRCAR, percepciones: no se pudo consultar. Sesión vencida' in resumen(row)
    result['estados']['SIRCUPA']['motivo'] = 'Consulta conjunta incompleta'
    assert '• SIRCUPA: no se pudo consultar. Consulta conjunta incompleta' in resumen(row)


def test_resumen_separa_regimenes_y_nunca_es_cero_sin_aclarar():
    text = resumen({"nombre_legal": "Empresa Sintética SA", "periodo": "09/2026", "estado": "terminada", "resultado": RESULT})
    assert "\nTerminada con faltantes.\n" in text
    assert "• SIRCREB: 4 filas · $ 1.500,00" in text
    assert "• SIRTAC: no se pudo consultar. SIFERE no respondió" in text and "día 12" in text
    assert "• SIRPEI: SIFERE no registra deducciones de SIRPEI para 09/2026." in text
    assert "• SIRCAR, retenciones: 1 fila ·" in text and "1 filas" not in text
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
        flow.rows = [{"estado":"terminada"}]
        base["autorizado"] = True
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        await ComarbDeduccionesEntregas(flow).poll(bot)
        assert bot.send_document.await_count == 1  # sólo el autorizado; el tercero ya estaba reclamado
        texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
        assert any("ya no tiene acceso" in t for t in texts)
        assert sum("Empresa Sintética SA" in t for t in texts) == 1
        assert flow.sql[1].startswith("SELECT console.fn_comarb_deducciones_entregada_telegram(9,7)")
    asyncio.run(scenario())


def test_entrega_revalida_despues_del_claim_y_no_revela_nombre():
    async def scenario():
        row={"id_lectura":9,"id_contribuyente":7,"telegram_id":42,"periodo":"09/2026",
             "estado":"terminada","nombre_legal":"Nombre que no debe revelarse","autorizado":True}
        flow=FakeFlow([[row],[True]],rows=[None])
        bot=SimpleNamespace(send_message=AsyncMock(),send_document=AsyncMock())
        await ComarbDeduccionesEntregas(flow).poll(bot)
        bot.send_document.assert_not_awaited()
        assert 'ya no tiene acceso' in bot.send_message.await_args.kwargs['text']
        assert row['nombre_legal'] not in bot.send_message.await_args.kwargs['text']
    asyncio.run(scenario())


def test_revocacion_durante_el_resumen_impide_enviar_excel(tmp_path):
    async def scenario():
        import hashlib
        relative = 'estudios/1/30000000011/2026/09/comarb/sintetica-deducciones.xlsx'
        excel = tmp_path / relative
        excel.parent.mkdir(parents=True)
        excel.write_bytes(b'PK synthetic')
        row = {'id_lectura':9,'id_contribuyente':7,'telegram_id':42,'periodo':'09/2026',
               'estado':'terminada','nombre_legal':'Empresa Sintética SA','autorizado':True,
               'resultado':RESULT,'ruta':relative,'sha256':hashlib.sha256(excel.read_bytes()).hexdigest()}
        flow = FakeFlow([[row],[True]], rows=[{'estado':'terminada'}])
        flow.clients_root = tmp_path
        async def revoke(**kwargs):
            flow.rows = [None]  # permiso retirado mientras Telegram confirma el resumen
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=revoke),send_document=AsyncMock())
        await ComarbDeduccionesEntregas(flow).poll(bot)
        bot.send_document.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('allowed,chat_type,dispatched',[(False,'private',False),(True,'group',False),(True,'private',True)])
@pytest.mark.parametrize('prefix,attribute',[('cd','_comarb_deducciones_flow'),('ar','_agip_deducciones_flow')])
def test_adapter_autentica_y_restringe_consulta_a_privado(allowed,chat_type,dispatched,prefix,attribute):
    async def scenario():
        adapter = TelegramAdapter(PlatformConfig(enabled=True,token='synthetic'))
        adapter.set_authorization_check(lambda *args, **kwargs: allowed)
        handler = SimpleNamespace(callback=AsyncMock())
        setattr(adapter,attribute,handler)
        query = _query(prefix+':uno')
        query.message = SimpleNamespace(chat_id=42,chat=SimpleNamespace(type=chat_type),message_thread_id=None)
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query),SimpleNamespace())
        assert handler.callback.await_count == int(dispatched)
        if dispatched:
            assert handler.callback.await_args.args[-1]=='42'
        else:
            query.answer.assert_awaited_once()
    asyncio.run(scenario())


def test_cancelar_no_toma_la_identidad_del_dueno_del_boton():
    async def scenario():
        flow=FakeFlow([[False],[True]])
        adapter=_adapter()
        await flow.callback(adapter,_query('cd:stop:9',84),'cd:stop:9',84,None,'84')
        await flow.callback(adapter,_query('cd:stop:9',42),'cd:stop:9',42,None,'42')
        # #191: tras cancelar, el aviso lee el estado con la misma identidad (la del que tocó).
        assert flow.sql[:2]==['SELECT console.fn_comarb_deducciones_cancelar_telegram(84,9)::text;',
                              'SELECT console.fn_comarb_deducciones_cancelar_telegram(42,9)::text;']
        assert all('(42,9)' in q for q in flow.sql[1:])
    asyncio.run(scenario())


def test_191_mismo_motivo_una_sola_vez():
    """Corrida 597: Santa Fe y Tucumán repetían el mismo motivo por categoría; Para SIFERE, el mismo detalle."""
    motivo_sf = "ARCA no tiene habilitado el servicio para la clave que se usó."
    estados = {r: {"estado": "sin_datos_verificado"} for r in flow_module.REGIMENES}
    estados.update({"Santa Fe · Percepciones": {"estado": "no_verificado", "motivo": motivo_sf},
                    "Santa Fe · Retenciones": {"estado": "no_verificado", "motivo": motivo_sf},
                    "ARBA · Percepciones": {"estado": "no_verificado", "motivo": "Otro motivo."}})
    falta = "Como Tucumán no se pudo consultar, no se prepara un archivo vacío."
    row = {"nombre_legal": "Contribuyente", "periodo": "10/2026", "estado": "terminada",
           "resultado": {"estados": estados, "importaciones_sifere": {"estados": [
               {"organismo": "ARBA", "tipo": "Percepciones", "estado": "no_verificado", "detalle": falta},
               {"organismo": "Tucumán", "tipo": "Retenciones", "estado": "no_verificado", "detalle": falta}]}}}
    text = resumen(row)
    assert text.count(motivo_sf) == 1
    assert "• Santa Fe · Retenciones y Santa Fe · Percepciones: no se pudo consultar. " + motivo_sf in text
    assert "• ARBA · Percepciones: no se pudo consultar. Otro motivo." in text
    # #191 (pedido 5): lo que no se pudo consultar se dice una sola vez, en su renglón; no se repite en Para SIFERE.
    assert falta not in text and "Para SIFERE" not in text


def test_191_resumen_por_organismo_en_el_orden_del_menu_y_faltantes_una_vez():
    """Lectura 614: ARBA quedó entre AGIP Retenciones y Percepciones, Tucumán partía a Santa Fe, y los faltantes
    se repetían en el encabezado, la lista y Para SIFERE."""
    estados = {r: {"estado": "sin_datos_verificado"} for r in flow_module.REGIMENES}
    estados["SIRCAR · Retenciones"] = {"estado": "con_datos", "filas": 1, "importe": "45491.22"}
    tuc = "No se consultó: cancelaste la corrida y esta fuente necesitaba un CAPTCHA, así que no se pidió."
    arba = "No hay una clave cargada."
    # Orden de lectura real de la 611/614: AGIP Ret, ARBA, AGIP Perc, Santa Fe Ret, Tucumán, Santa Fe Perc.
    for r, e in (("AGIP · Retenciones", {"estado": "con_datos", "filas": 82, "importe": "1"}),
                 ("ARBA · Retenciones", {"estado": "no_verificado", "motivo": arba}),
                 ("ARBA · Percepciones", {"estado": "no_verificado", "motivo": arba}),
                 ("AGIP · Percepciones", {"estado": "con_datos", "filas": 38, "importe": "1"}),
                 ("Santa Fe · Retenciones", {"estado": "con_datos", "filas": 2, "importe": "1"}),
                 ("Tucumán · Percepciones", {"estado": "no_verificado", "motivo": tuc}),
                 ("Tucumán · Retenciones", {"estado": "no_verificado", "motivo": tuc}),
                 ("Santa Fe · Percepciones", {"estado": "sin_datos_verificado"})):
        estados[r] = e
    falta = "Como ARBA no se pudo consultar, no se prepara un archivo vacío."
    text = resumen({"nombre_legal": "Contribuyente", "periodo": "10/2026", "estado": "terminada",
                    "resultado": {"estados": estados, "posibles_duplicados": 1, "importaciones_sifere": {"estados": [
                        {"organismo": "ARBA", "tipo": "Retenciones", "estado": "no_verificado", "detalle": falta}]}}})
    lineas = [l for l in text.split("\n") if l.startswith("• ")]
    orden = [l.split(":")[0][2:] for l in lineas]
    assert orden == ["AGIP · Retenciones", "AGIP · Percepciones", "ARBA · Retenciones y ARBA · Percepciones",
                     "Tucumán · Retenciones y Tucumán · Percepciones", "Santa Fe · Retenciones", "Santa Fe · Percepciones",
                     "SIRCREB", "SIRCUPA", "SIRTAC", "SIRPEI", "SIRCAR, retenciones", "SIRCAR, percepciones"]
    assert "\nTerminada con faltantes.\n" in text and text.count("ARBA · Retenciones") == 1 and falta not in text
    assert "• SIRCAR, retenciones: 1 fila · $ 45.491,22" in text
    assert "1 fila marcada como posible duplicado" in text


def test_191_captcha_sin_atribuirlo_a_arca():
    """Mientras espera el CAPTCHA de Rentas Tucumán, el avance decía «ARCA pidió un CAPTCHA»."""
    assert "ARCA" not in flow_module.PROGRESO["captcha_telegram"]
    assert "ARCA" not in flow_module.PROGRESO["consulta_en_curso"]
