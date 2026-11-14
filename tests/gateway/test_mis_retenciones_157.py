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
    assert mr.periodo('01/09/2026 - 30/09/2026') == ('01/09/2026', '30/09/2026')
    for texto, motivo in (('2026-09', 'MM/AAAA'), ('31/02/2026 a 01/03/2026', 'no existe'),
                          ('01/10/2026 a 01/09/2026', 'anterior')):
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
    assert [b[0].callback_data for b in impuestos[:-1]] == [f'pi:mri:{"a"*10}:{k}' for k in ('iva', 'ganancias', 'bienes_personales', 'suss')]
    assert [b[0].text for b in impuestos[:-1]] == ['IVA', 'Ganancias', 'Bienes Personales', 'Seguridad social']
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


# --- Ampliación del 14/11/2026: 787, Bienes Personales, IVA no frena lo demás, resumen una sola vez ---

def _teclados(monkeypatch):
    monkeypatch.setattr(mr, 'InlineKeyboardButton', lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
    monkeypatch.setattr(mr, 'InlineKeyboardMarkup', lambda rows: SimpleNamespace(inline_keyboard=rows))


def test_ganancias_son_217_y_787_y_bienes_personales_219():
    assert mr.IMPUESTOS['ganancias'] == ('Ganancias', ('217', '787'))
    assert mr.IMPUESTOS['bienes_personales'] == ('Bienes Personales', ('219',))
    assert list(mr.IMPUESTOS) == ['iva', 'ganancias', 'bienes_personales', 'suss']


def test_bienes_personales_sigue_el_mismo_flujo(monkeypatch):
    _teclados(monkeypatch)
    flow, s = Flow(), state()
    flow.states['k'] = s
    query = SimpleNamespace(answer=lambda *a, **k: asyncio.sleep(0))
    assert asyncio.run(mr.callback(flow, None, query, f'pi:mri:{"a"*10}:bienes_personales', 1, None, 'k'))
    assert (s.mr_impuesto, s.stage) == ('bienes_personales', 'mr_tipo')
    assert flow.sent[-1][0] == 'Elegí el tipo de operación.'


@pytest.mark.parametrize('iva,fragmento', [('monotributo', 'monotributista'), ('sin_inscripcion_verificada', 'inscripción en IVA verificada')])
def test_no_inscripto_en_iva_no_frena_los_demas_impuestos(monkeypatch, iva, fragmento):
    _teclados(monkeypatch)
    flow, s = Flow(), state(scope_item={'iva': iva})
    flow.states['k'] = s
    answers = []
    query = SimpleNamespace(answer=lambda text: answers.append(text) or asyncio.sleep(0))
    assert asyncio.run(mr.callback(flow, None, query, f'pi:mri:{"a"*10}:iva', 1, None, 'k'))
    # IVA: no corresponde, con su motivo; no se inicia nada y se vuelve a ofrecer el impuesto.
    assert (s.mr_impuesto, s.stage) == (None, 'mr_impuesto')
    texto, teclado = flow.sent[-1]
    assert texto.startswith('IVA: no corresponde.') and fragmento in texto and 'Ganancias, Bienes Personales o Seguridad social' in texto
    assert [b[0].callback_data.rsplit(':', 1)[1] for b in teclado.inline_keyboard[:-1]] == ['iva', 'ganancias', 'bienes_personales', 'suss']
    # Los demás impuestos se consultan igual.
    assert asyncio.run(mr.callback(flow, None, query, f'pi:mri:{"a"*10}:ganancias', 1, None, 'k'))
    assert (s.mr_impuesto, s.stage) == ('ganancias', 'mr_tipo')


def test_inscripto_en_iva_consulta_iva_como_siempre(monkeypatch):
    _teclados(monkeypatch)
    flow, s = Flow(), state(scope_item={'iva': 'corresponde'})
    flow.states['k'] = s
    query = SimpleNamespace(answer=lambda *a, **k: asyncio.sleep(0))
    assert asyncio.run(mr.callback(flow, None, query, f'pi:mri:{"a"*10}:iva', 1, None, 'k'))
    assert (s.mr_impuesto, s.stage) == ('iva', 'mr_tipo')


def test_resultado_de_ganancias_trae_los_dos_codigos_en_orden():
    s = state(mr_impuesto='ganancias', mr_tipo='ambas', mr_desde='01/09/2026', mr_hasta='30/09/2026')
    cero = {'estado': 'cero', 'cantidad_pantalla': 0, 'total_pantalla': '0.00', 'motivo_texto': 'ARCA informó que no hay resultados para este filtro.'}
    dos = resultado(impuesto='ganancias', xlsx=None, consultas=[{'codigo': '217', 'etiqueta': '217 - SICORE-IMPTO.A LAS GANANCIAS', **cero},
                                                                {'codigo': '787', 'etiqueta': '787 - RET ART 79 LEY GCIAS INC A,ByC', **cero}])
    consultas, excel, archivos = mr.validate_result(Flow(), s, dos)
    assert [c['codigo'] for c in consultas] == ['217', '787'] and excel is None and archivos == []
    with pytest.raises(RuntimeError, match='CONSULTAS_NO_COINCIDEN'):
        mr.validate_result(Flow(), s, {**dos, 'consultas': list(reversed(dos['consultas']))})


class _Historial:
    def __init__(self):
        self.items = []; self.cerradas = 0

    async def start(self, **kw):
        return 'corrida'

    async def prepare_items(self, handle, refs):
        self.refs = refs
        return [{'id_item': i + 1} for i, _ in enumerate(refs)]

    async def attach_run(self, *a, **k):
        pass

    async def finish_item(self, handle, item, **kw):
        self.items.append((item, kw['state'], kw['reason_text']))

    async def close(self, handle):
        self.cerradas += 1


class _FlowCompleto(Flow):
    def __init__(self, tmp, result):
        super().__init__()
        self.history = _Historial(); self.result = result; self.tmp = tmp
        self.execution_locks = {}; self.processes = {}; self.clients_root = tmp; self.uv = Path('/usr/bin/uv')
        self.ediciones = []

    def available(self):
        return True

    def _scope(self, operation=None):
        return SimpleNamespace(require_permission=lambda *a: None)

    def _by_id(self, item_id, telegram_id, operation=None):
        self.operacion_consultada = operation
        return [dict(self.binding)]

    def _acquire_execution_lock(self, lock):
        pass

    def _release_execution_lock(self, lock):
        pass

    async def _communicate_with_progress(self, proc, state, **kw):
        return b'{}', b''

    def _parse_result(self, raw):
        return self.result

    def _saved(self, path, ruta, sha=None):
        return Path(path)

    @staticmethod
    def _stage_delivery_files(deliverables):
        return Path('/nonexistent-staging'), deliverables

    async def _edit_progress(self, state, text, *, keyboard=None):
        self.ediciones.append(text)

    async def _terminate_process(self, proc):
        pass


def _corrida(monkeypatch, tmp_path, impuesto, consultas, xlsx=None, iva='corresponde'):
    excel = tmp_path / 'sintetico_mis_retenciones_ganancias_202609.xlsx'
    excel.write_bytes(b'x')
    result = resultado(impuesto=impuesto, consultas=consultas, xlsx={'ruta': str(excel), 'ruta_clientes': 'x', 'sha256': 'y'} if xlsx else None)
    flow = _FlowCompleto(tmp_path, result)
    (tmp_path / 'mis_retenciones.py').write_text('')
    flow.executor = tmp_path / 'portal_iva.py'
    flow.binding = {'id': 7, 'slug': 'sintetico', 'cuit': '20000000001', 'study_id': 1, 'relation_id': 2, 'relation_revision': 3,
                    'access_id': 4, 'representative_id': 7, 'iva': iva}
    s = state(mr_impuesto=impuesto, mr_tipo='ambas', mr_desde='01/09/2026', mr_hasta='30/09/2026', stage='running', user_id='99',
              period='2026-09', scope_item=dict(flow.binding), cancelled=False, progress_message=object())
    enviados = []

    async def send_document(**kw):
        enviados.append(kw['file_name'])
        return SimpleNamespace(success=True, message_id='1', delivered_filename=kw['file_name'])

    async def proceso(*a, **k):
        return SimpleNamespace(returncode=0)

    async def verificada(*a, **k):
        return None
    monkeypatch.setattr(mr.asyncio, 'create_subprocess_exec', proceso)
    monkeypatch.setattr(mr, 'verify_representation', verificada)
    monkeypatch.setattr(mr.shutil, 'rmtree', lambda *a, **k: None)
    asyncio.run(mr.run(flow, SimpleNamespace(send_document=send_document), 1, None, 'k', s))
    return flow, enviados


def test_el_resumen_final_llega_una_sola_vez(monkeypatch, tmp_path):
    cero = {'estado': 'cero', 'cantidad_pantalla': 0, 'total_pantalla': '0.00', 'motivo_texto': 'ARCA informó que no hay resultados para este filtro.'}
    consultas = [{'codigo': '217', 'etiqueta': '217 - SICORE-IMPTO.A LAS GANANCIAS', 'estado': 'completado', 'cantidad_pantalla': 2, 'total_pantalla': '10.00',
                  'archivo': {'ruta': str(tmp_path / 'sintetico_mis_retenciones_217_202609_original.xlsx'), 'ruta_clientes': 'x', 'sha256': 'y'}},
                 {'codigo': '787', 'etiqueta': '787 - RET ART 79 LEY GCIAS INC A,ByC', **cero}]
    flow, enviados = _corrida(monkeypatch, tmp_path, 'ganancias', consultas, xlsx=True)
    resumen = [t for t, _ in flow.sent if '217 - SICORE-IMPTO.A LAS GANANCIAS: 2 registros' in t]
    # Un solo mensaje con el detalle; el de avance se cierra con una línea corta y distinta.
    assert len(resumen) == 1 and '787 - RET ART 79 LEY GCIAS INC A,ByC: ARCA informó que no hay resultados' in resumen[0]
    assert flow.ediciones == ['Sintético · Mis Retenciones Ganancias: consulta terminada.']
    assert resumen[0] not in flow.ediciones
    # Cada código con su línea en la bitácora y el mismo nombre de archivo que en el panel.
    assert flow.history.refs == ['Mis Retenciones 217 · 01/09/2026 a 30/09/2026', 'Mis Retenciones 787 · 01/09/2026 a 30/09/2026']
    assert [e for _, e, _ in flow.history.items] == ['completado', 'completado']
    assert enviados == ['sintetico_mis_retenciones_ganancias_202609.xlsx', 'sintetico_mis_retenciones_217_202609_original.xlsx']
    assert flow.operacion_consultada == 'mis-retenciones'


def test_sin_datos_no_falla_y_tambien_avisa_una_sola_vez(monkeypatch, tmp_path):
    cero = {'codigo': '219', 'etiqueta': '219 - SICORE-IMPTO.S/ BS PERSONALES', 'estado': 'cero', 'cantidad_pantalla': 0, 'total_pantalla': '0.00',
            'motivo_texto': 'ARCA informó que no hay resultados para este filtro.'}
    flow, enviados = _corrida(monkeypatch, tmp_path, 'bienes_personales', [cero])
    finales = [t for t, _ in flow.sent if 'Bienes Personales' in t]
    assert len(finales) == 1 and 'No se descargó ningún archivo.' in finales[0] and enviados == []
    assert [e for _, e, _ in flow.history.items] == ['completado']


def test_iva_no_corresponde_tampoco_se_consulta_si_cambio_despues_de_elegir(monkeypatch, tmp_path):
    flow, enviados = _corrida(monkeypatch, tmp_path, 'iva', resultado()['consultas'], iva='monotributo')
    assert enviados == [] and len(flow.sent) == 1
    assert flow.sent[0][0].startswith('IVA: no corresponde.') and 'No se consultó ARCA' in flow.sent[0][0]
    assert {e for _, e, _ in flow.history.items} == {'fallido'}
