"""#191: elegir clientes de una lista, cartera confirmada, cancelar ordenado y Preparar por lote."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.platforms.telegram.accesos_texto import como_comprobar
from plugins.platforms.telegram.agip_deducciones_flow import AgipDeduccionesFlow
from plugins.platforms.telegram.comarb_deducciones_flow import CANCELADA_ANTES, ComarbDeduccionesEntregas, ComarbDeduccionesFlow
from plugins.platforms.telegram.lote_selector import Lote, resumen_no_incluidos
from plugins.platforms.telegram.preparar_lote_flow import PrepararLoteFlow
from plugins.platforms.telegram.provinciales_iibb import ProvincialDeduccionesFlow
from tests.gateway.period_helpers import confirm_period

import pytest


@pytest.fixture(autouse=True)
def teclados(monkeypatch):
    """El conftest reemplaza telegram por un mock: botones y teclados simples, como en las otras pruebas."""
    import plugins.platforms.telegram.lote_selector as ls
    import plugins.platforms.telegram.comarb_deducciones_flow as cdf
    for module in (ls, cdf):
        monkeypatch.setattr(module, 'InlineKeyboardButton', lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
        monkeypatch.setattr(module, 'InlineKeyboardMarkup', lambda rows: SimpleNamespace(inline_keyboard=rows))


FILAS = [{'id': i, 'nombre': f'Empresa Sintética {i:02d}', 'cuit': f'300000000{i:02d}', 'asignado': i in (3, 4)} for i in range(1, 13)]


class Fake:
    def __init__(self, answers):
        self.sql = []; self.answers = answers

    def _write(self, sql):
        self.sql.append(sql); return self.answers.pop(0)

    def _query(self, sql):
        self.sql.append(sql); return self.answers.pop(0)


def flujo(cls, answers, **kw):
    f = cls(**kw, query_connection=lambda: None, write_connection=lambda: None)
    fake = Fake(answers)
    f._write, f._query = fake._write, fake._query
    f._scope = lambda: SimpleNamespace(require_actor=lambda u: None, require_permission=lambda u, p: None)
    f.watchers_started = []
    f._watch = AsyncMock()
    return f, fake


def query(data, user=42):
    return SimpleNamespace(data=data, answer=AsyncMock(), from_user=SimpleNamespace(id=user),
                           edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())


def adapter():
    return SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock()))


def msg(text, user=42):
    return SimpleNamespace(text=text, chat_id=user, from_user=SimpleNamespace(id=user), message_thread_id=None,
                           reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock(), reply_markup=None)))


def botones(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def test_selector_filtra_tilda_marca_visibles_mis_clientes_y_tope():
    lote = Lote.desde_filas('ab12cd34', FILAS, maximo=5)
    assert lote.paginas == 2 and len(lote.visibles) == 12
    lote.filtrar('sintetica 1')  # sin tilde y sin mayúsculas
    assert [i['id'] for i in lote.visibles] == [10, 11, 12]
    lote.accion('a')
    assert lote.marcados == {10, 11, 12}
    lote.filtrar('3000000000')  # por CUIT
    assert len(lote.visibles) == 9
    assert lote.accion('a') == 'Marqué 2: hasta 5 clientes por corrida.'
    assert len(lote.marcados) == 5
    lote.accion('m')
    assert lote.marcados == {3, 4}  # «Mis clientes» = asignados
    lote.accion('t', '3'); lote.accion('t', '99')  # ajeno a la lista: no se tilda
    assert lote.marcados == {4}
    datos = [cb for _, cb in botones(lote.teclado('cd'))]
    assert all(len(cb.encode()) <= 64 for cb in datos)
    assert 'cd:lm:ab12cd34' in datos and 'cd:lc:ab12cd34' in datos and 'cd:lf:ab12cd34' in datos


def test_no_incluidos_acotado_y_sin_cuit():
    texto = resumen_no_incluidos([{'nombre': f'N{i}', 'motivo': 'Sin acceso.'} for i in range(12)], limite=3)
    assert texto.startswith('No incluidos (12):') and '… y 9 más' in texto


def test_lote_de_deducciones_corre_solo_los_tildados():
    async def scenario():
        f, fake = flujo(ComarbDeduccionesFlow, [FILAS, [{'estado': 'encolada', 'id_lectura': 5, 'solicitados': 2,
            'no_incluidos': [{'nombre': 'Empresa Sintética 03', 'motivo': 'No tiene un acceso utilizable para las fuentes del período.'}]}]])
        a = adapter()
        await f.callback(a, query('cd:lote'), 'cd:lote', 42, None, '42')
        assert fake.sql[0] == "SELECT row_to_json(x)::text FROM console.fn_lote_contribuyentes_telegram(42,'deducciones') x;"
        state = f.states[f._key(42, None, '42')]
        for cid in ('2', '3', '7'):
            await f.callback(a, query(f'cd:lt:{state.nonce}:{cid}'), f'cd:lt:{state.nonce}:{cid}', 42, None, '42')
        await f.callback(a, query(f'cd:lt:{state.nonce}:7'), f'cd:lt:{state.nonce}:7', 42, None, '42')  # destildar
        await f.callback(a, query(f'cd:lc:{state.nonce}'), f'cd:lc:{state.nonce}', 42, None, '42')
        assert '2 clientes tildados' in a._bot.send_message.await_args.kwargs['text']
        m = msg('07/2026'); await f.text(a, m)
        await confirm_period(f, a, 42, '42')
        assert fake.sql[-1] == "SELECT console.fn_comarb_deducciones_lote_telegram(42,'07/2026','deducciones',ARRAY[2,3]::bigint[])::text;"
        texto = m.reply_text.await_args.args[0]
        assert 'Lote de 2 contribuyentes' in texto  # detalle de la 619: la palabra del panel and 'No incluidos (1)' in texto
        assert botones(m.reply_text.await_args.kwargs['reply_markup']) == [('❌  Cancelar', 'cd:stop:5')]
    asyncio.run(scenario())


def test_otro_nonce_o_lista_vieja_no_tilda():
    async def scenario():
        f, _ = flujo(ComarbDeduccionesFlow, [FILAS])
        a = adapter()
        await f.callback(a, query('cd:lote'), 'cd:lote', 42, None, '42')
        q = query('cd:lt:00000000:2')
        await f.callback(a, q, 'cd:lt:00000000:2', 42, None, '42')
        assert not f.states[f._key(42, None, '42')].lote.marcados
        assert 'ya no está vigente' in q.answer.await_args.args[0]
    asyncio.run(scenario())


def test_agip_y_provincia_encolan_con_su_operacion():
    async def scenario():
        for cls, kw, op, prefix in ((AgipDeduccionesFlow, {}, 'agip_deducciones', 'ar'),
                                    (ProvincialDeduccionesFlow, {'codigo': 'tucuman'}, 'tucuman_deducciones', 'tu')):
            f, fake = flujo(cls, [FILAS, [{'estado': 'encolada', 'id_lectura': 6, 'solicitados': 1}]], **kw)
            a = adapter()
            await f.callback(a, query(f'{prefix}:lote'), f'{prefix}:lote', 42, None, '42')
            # #191 (prueba final): la lista es la de su propia consulta (sólo clientes con IIBB de ese régimen).
            assert fake.sql[0] == f"SELECT row_to_json(x)::text FROM console.fn_lote_contribuyentes_telegram(42,'{op}') x;"
            n = f.states[f._key(42, None, '42')].nonce
            await f.callback(a, query(f'{prefix}:lt:{n}:4'), f'{prefix}:lt:{n}:4', 42, None, '42')
            await f.callback(a, query(f'{prefix}:lc:{n}'), f'{prefix}:lc:{n}', 42, None, '42')
            await f.text(a, msg('07/2026')); await confirm_period(f, a, 42, '42')
            assert fake.sql[-1] == f"SELECT console.fn_comarb_deducciones_lote_telegram(42,'07/2026','{op}',ARRAY[4]::bigint[])::text;"
    asyncio.run(scenario())


def test_cancelar_avisa_que_termina_el_cliente_en_curso():
    async def scenario():
        f, fake = flujo(ComarbDeduccionesFlow, [[True], [{'estado': 'consultando', 'solicitados': 3, 'actual': {'contribuyente': 'Empresa Sintética 01'}}]])
        a = adapter(); q = query('cd:stop:9')
        await f.callback(a, q, 'cd:stop:9', 42, None, '42')
        assert fake.sql == ['SELECT console.fn_comarb_deducciones_cancelar_telegram(42,9)::text;',
                            'SELECT console.fn_comarb_deducciones_estado_telegram(42,9)::text;']
        assert q.answer.await_args.args[0] == 'Cancelando: corto la consulta de Empresa Sintética 01, que no se entrega, y no sigo con los demás.'
        q.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    asyncio.run(scenario())


def test_cancelar_sin_cliente_en_curso_no_promete_terminar_nada(monkeypatch):
    """#191 (consulta 610): los tres se cancelaron sin empezar; ni el aviso ni el cierre hablan de algo en curso o terminado."""
    async def scenario():
        f, _ = flujo(ComarbDeduccionesFlow, [[True], [{'estado': 'cancelada', 'solicitados': 3, 'terminados': 3, 'cancelados': 3, 'actual': None}]])
        q = query('cd:stop:9')
        await f.callback(adapter(), q, 'cd:stop:9', 42, None, '42')
        aviso = q.answer.await_args.args[0]
        assert aviso == 'Cancelada: no se consulta ningún contribuyente más.'
        g = ComarbDeduccionesFlow(query_connection=lambda: None, write_connection=lambda: None)
        g._query = lambda sql: [{'estado': 'cancelada', 'solicitados': 3, 'terminados': 3, 'cancelados': 3, 'actual': None}]
        monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
        message = SimpleNamespace(edit_text=AsyncMock(), reply_markup='botón')
        await g._watch(message, 42, 9, 'Cabecera', '')
        texto = message.edit_text.await_args.args[0]
        assert texto == 'Cabecera\nCancelada: no terminó ninguno de los 3; 3 no llegaron a empezar. Quedan en Operaciones.'
        assert 'Te mando' not in texto and 'en curso' not in texto
    asyncio.run(scenario())


def test_avance_muestra_cancelacion_y_cierre_con_los_no_consultados(monkeypatch):
    async def scenario():
        f = ComarbDeduccionesFlow(query_connection=lambda: None, write_connection=lambda: None)
        estados = [[{'estado': 'consultando', 'solicitados': 3, 'terminados': 0, 'cancelacion_solicitada': True, 'cancelados': 0,
                     'actual': {'contribuyente': 'Empresa Sintética 01', 'progreso': 'Leyendo SIFERE'}}],
                   [{'estado': 'cancelada', 'solicitados': 4, 'terminados': 4, 'cancelados': 3, 'cortados': 1, 'actual': None}]]
        f._query = lambda sql: estados.pop(0)
        monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
        message = SimpleNamespace(edit_text=AsyncMock(), reply_markup='botón')
        await f._watch(message, 42, 9, 'Cabecera', '')
        textos = [c.args[0] for c in message.edit_text.await_args_list]
        assert textos[0] == 'Cabecera\nCancelando: corto la consulta de Empresa Sintética 01, que no se entrega.'
        assert message.edit_text.await_args_list[0].kwargs['reply_markup'] is None
        # Lectura 614: un único aviso; lo terminado ya se mandó cliente por cliente.
        assert textos[1] == ('Cabecera\nCancelada: 1 de 4 terminó y ya te mandé su resultado; se cortó la consulta en curso '
                             'y no se entrega; 2 no llegaron a empezar. Quedan en Operaciones.')
    asyncio.run(scenario())


def test_los_no_consultados_por_cancelar_no_mandan_un_mensaje_cada_uno():
    async def scenario():
        f = ComarbDeduccionesFlow(query_connection=lambda: None, write_connection=lambda: None)
        # Lectura 614: tampoco el cliente cortado en curso; el aviso único de la cancelación los cuenta.
        filas = [{'telegram_id': 42, 'id_lectura': 9, 'id_contribuyente': 2, 'estado': 'cancelada', 'motivo': CANCELADA_ANTES,
                  'periodo': '07/2026', 'nombre_legal': 'Empresa Sintética 02', 'resultado': {}, 'autorizado': True},
                 {'telegram_id': 42, 'id_lectura': 9, 'id_contribuyente': 1, 'estado': 'cancelada',
                  'motivo': 'Se cortó al cancelar la corrida: no se entrega ningún resultado de este contribuyente.',
                  'periodo': '07/2026', 'nombre_legal': 'Empresa Sintética 01', 'resultado': {'codigo': 'consulta_cancelada'}, 'autorizado': True}]
        respuestas = [filas, [True], [True]]
        f._write = lambda sql: respuestas.pop(0)
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        await ComarbDeduccionesEntregas(f).poll(bot)
        bot.send_message.assert_not_awaited()
    asyncio.run(scenario())


def test_preparar_por_lote_encola_tildados_por_periodo_y_explica_los_que_no():
    async def scenario():
        filas = [{'id': 1, 'nombre': 'RI Sintético', 'cuit': '30000000017', 'elegible': True},
                 {'id': 2, 'nombre': 'Mono Sintético', 'cuit': '20000000017', 'elegible': False, 'motivo': 'monotributo'}]
        cfg=dict(faltantes=[],valor=dict(prorrateo=False,modalidad=None,importador=False,tipo_importacion=None,exportador=False))
        f, fake = flujo(PrepararLoteFlow, [filas, [[dict(nombre='RI Sintético',configuracion=cfg)]], [{'estado': 'encolada', 'id_lectura': 8, 'solicitados': 1,
            'no_incluidos': [{'id': 2, 'nombre': 'Mono Sintético', 'motivo': 'monotributo'}]}]])
        a = adapter()
        await f.callback(a, query('pp:lote'), 'pp:lote', 42, None, '42')
        assert fake.sql[0] == "SELECT row_to_json(x)::text FROM console.fn_lote_contribuyentes_telegram(42,'preparar') x;"
        lista = a._bot.send_message.await_args.kwargs
        # Lectura 614 (pedido 2): sin la leyenda «N no se pueden elegir…»; la lista muestra sólo los elegibles.
        assert 'no se pueden elegir' not in lista['text']
        assert [cb for _, cb in botones(lista['reply_markup']) if ':lt:' in cb] == [f"pp:lt:{f.states[f._key(42, None, '42')].nonce}:1"]
        n = f.states[f._key(42, None, '42')].nonce
        await f.callback(a, query(f'pp:la:{n}'), f'pp:la:{n}', 42, None, '42')
        await f.callback(a, query(f'pp:lc:{n}'), f'pp:lc:{n}', 42, None, '42')
        m = msg('10/2026'); await f.text(a, m); await confirm_period(f, a, 42, '42')
        assert fake.sql[-1] == "SELECT console.fn_portal_iva_preparar_lote_telegram_v2(42,'10/2026',ARRAY[1]::bigint[])::text;"
        texto = m.reply_text.await_args.args[0]
        assert 'Preparar período nuevo' in texto and 'En espera de Preparar período nuevo.' in texto
        assert 'monotributista' in texto
    asyncio.run(scenario())


def test_aviso_de_acceso_dice_que_boton_tocar():
    texto = como_comprobar('ARCA')
    assert '«Comprobar»' in texto and 'Accesos fiscales' in texto and 'Volver a cargar la clave no la comprueba' in texto
    from plugins.platforms.telegram.comarb_deducciones_flow import RECHAZOS
    from plugins.platforms.telegram.portal_iva_flow import _IVA_INELIGIBLE
    assert '«Comprobar»' in _IVA_INELIGIBLE['clave_sin_verificar']
    # #191 (pedido 8): en Deducciones la clave se comprueba sola al consultar; sólo falta cargarla o corregirla.
    assert 'se comprueba sola al consultar' in RECHAZOS['clave_sin_verificar'] and 'Accesos fiscales' in RECHAZOS['clave_sin_verificar']


def test_captcha_de_preparar_tiene_su_texto():
    from plugins.platforms.telegram.panel_captcha_bridge import CAPTIONS
    assert CAPTIONS['Portal IVA'].startswith('Preparar período nuevo')
