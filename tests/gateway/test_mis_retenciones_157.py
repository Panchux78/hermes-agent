"""#157 Mis Retenciones por Telegram: período, pasos, contrato del resultado y entrega."""
import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.platforms.telegram.mis_retenciones_flow as mr


def test_mes_y_rango_con_motivo_concreto():
    assert mr.periodo('09/2026') == ('01/09/2026', '30/09/2026')
    assert mr.periodo('2/2024') == ('01/02/2024', '29/02/2024')
    assert mr.periodo(' 01/01/2019  a 30/09/2026 ') == ('01/01/2019', '30/09/2026')
    for texto, motivo in (('2026-09', 'No entendí'), ('31/02/2026 a 01/03/2026', 'no existe'),
                          ('01/10/2026 a 01/09/2026', 'anterior'), ('01/09/2026 - 30/09/2026', 'No entendí')):
        with pytest.raises(ValueError, match=motivo):
            mr.periodo(texto)
    manana = (date.today() + timedelta(days=1)).strftime('%d/%m/%Y')
    with pytest.raises(ValueError, match='posterior a hoy'):
        mr.periodo(f'01/01/2026 a {manana}')


def test_progreso_por_codigo():
    assert mr.progress('mr:767:exportacion') == '767 · Esperando el archivo de ARCA…'
    assert mr.progress('mr:ingreso') == 'Ingresando a ARCA…'
    assert mr.progress('mr:999:otra') is None


def test_teclados_con_callback_corto_y_cancelar(monkeypatch):
    monkeypatch.setattr(mr, 'InlineKeyboardButton', lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
    monkeypatch.setattr(mr, 'InlineKeyboardMarkup', lambda rows: SimpleNamespace(inline_keyboard=rows))
    impuestos = mr.impuesto_keyboard('a' * 10).inline_keyboard
    assert [b[0].callback_data for b in impuestos[:-1]] == [f'pi:mri:{"a"*10}:{k}' for k in ('iva', 'ganancias', 'suss')]
    assert impuestos[-1][0].callback_data == f'pi:cancel:{"a"*10}'
    assert all(len(b[0].callback_data.encode()) <= 64 for b in impuestos + mr.tipo_keyboard('a' * 10).inline_keyboard)


class Flow:
    def __init__(self):
        self.sent = []
        self.states = {}
        self.executor = Path('/x/scripts/portal-iva/portal_iva.py')

    async def _send(self, adapter, chat, text, thread=None, markup=None):
        self.sent.append((text, markup))

    @staticmethod
    def _cancel_keyboard(nonce):
        return f'cancel:{nonce}'

    def _saved(self, path, ruta, sha):
        return Path(path)


def state(**kw):
    base = dict(nonce='a' * 10, stage='mr_impuesto', nombre='Sintético', slug='sintetico', cuit='20000000001',
                contributor_id=7, mr_impuesto=None, mr_tipo=None, mr_desde=None, mr_hasta=None)
    return SimpleNamespace(**{**base, **kw})


def test_seguridad_social_salta_tipo_y_pide_periodo(monkeypatch):
    monkeypatch.setattr(mr, 'InlineKeyboardButton', lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
    monkeypatch.setattr(mr, 'InlineKeyboardMarkup', lambda rows: SimpleNamespace(inline_keyboard=rows))
    flow, s = Flow(), state()
    flow.states['k'] = s
    query = SimpleNamespace(answer=lambda *a, **k: asyncio.sleep(0))
    assert asyncio.run(mr.callback(flow, None, query, f'pi:mri:{"a"*10}:suss', 1, None, 'k'))
    assert (s.mr_impuesto, s.mr_tipo, s.stage) == ('suss', 'retencion', 'mr_period')
    assert 'para aplicativo' in flow.sent[-1][0]


def test_opcion_vencida_no_avanza():
    flow, s = Flow(), state(stage='mr_period')
    flow.states['k'] = s
    answers = []
    query = SimpleNamespace(answer=lambda text: answers.append(text) or asyncio.sleep(0))
    assert asyncio.run(mr.callback(flow, None, query, f'pi:mrt:{"a"*10}:ambas', 1, None, 'k'))
    assert s.mr_tipo is None and 'venció' in answers[0]


def resultado(**kw):
    base = {'ok': True, 'operacion': 'arca_mis_retenciones', 'slug': 'sintetico', 'cuit': '20000000001',
            'id_contribuyente': 7, 'escrituras_externas': False, 'escrituras_confirmadas': [], 'impuesto': 'iva',
            'tipo': 'ambas', 'desde': '01/09/2026', 'hasta': '30/09/2026',
            'consultas': [{'codigo': '767', 'etiqueta': '767 - SICORE', 'estado': 'completado', 'cantidad_pantalla': 2,
                           'total_pantalla': '10.00', 'archivo': {'ruta': '/c/a-original-arca.xlsx', 'ruta_clientes': 'x', 'sha256': 'y'}},
                          {'codigo': '216', 'etiqueta': '216 - SIRE - IVA', 'estado': 'cero', 'cantidad_pantalla': 0,
                           'total_pantalla': '0.00', 'motivo_texto': 'ARCA informó que no hay resultados para este filtro.'}],
            'xlsx': {'ruta': '/c/derivado.xlsx', 'ruta_clientes': 'x', 'sha256': 'y'}}
    return {**base, **kw}


def test_resultado_tiene_que_ser_lo_pedido():
    s = state(mr_impuesto='iva', mr_tipo='ambas', mr_desde='01/09/2026', mr_hasta='30/09/2026')
    consultas, excel, archivos = mr.validate_result(Flow(), s, resultado())
    assert excel == Path('/c/derivado.xlsx') and [p.name for p, _ in archivos] == ['a-original-arca.xlsx']
    for cambio, code in (({'cuit': '20000000002'}, 'NO_COINCIDE'), ({'hasta': '31/09/2026'}, 'NO_COINCIDE'),
                         ({'xlsx': None}, 'EXCEL_NO_COINCIDE'), ({'ok': False, 'motivo': 'LOGIN_CREDENCIALES_RECHAZADAS'}, 'LOGIN_CREDENCIALES')):
        with pytest.raises(RuntimeError, match=code):
            mr.validate_result(Flow(), s, resultado(**cambio))
    sin_motivo = resultado()
    sin_motivo['consultas'][1].pop('motivo_texto')
    with pytest.raises(RuntimeError, match='ESTADO_INVALIDO'):
        mr.validate_result(Flow(), s, sin_motivo)


def test_linea_informa_total_y_cero_sin_confundirlos():
    c = resultado()['consultas']
    assert mr.linea(c[0]) == '767 - SICORE: 2 registros, total $ 10,00 (igual que la pantalla de ARCA).'
    assert 'no hay resultados' in mr.linea(c[1])
