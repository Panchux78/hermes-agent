"""#158 Mis Comprobantes por Telegram: período, pasos, contrato del resultado, fuente de compras y entrega."""
import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.platforms.telegram.mis_comprobantes_flow as mc
from plugins.platforms.telegram.period_input import parse_period


def test_mes_anio_y_rango_por_el_componente_comun():
    hoy = date(2026, 11, 15)
    sel = lambda t: parse_period(t, 'mis-comprobantes', today=hoy)
    assert mc.rango_efectivo(sel('07/2026'), hoy) == ('01/07/2026', '31/07/2026')
    assert mc.rango_efectivo(sel('2025'), hoy) == ('01/01/2025', '31/12/2025')
    # Año bisiesto completo: entra en el tope de ARCA y no pierde el 29/02.
    assert mc.rango_efectivo(sel('2024'), hoy) == ('01/01/2024', '31/12/2024')
    assert mc.rango_efectivo(sel('15/07/2026 a 10/08/2026'), hoy) == ('15/07/2026', '10/08/2026')
    # Mes y año en curso llegan hasta hoy: ARCA no acepta un rango que termine en el futuro.
    assert mc.rango_efectivo(sel('11/2026'), hoy) == ('01/11/2026', '15/11/2026')
    assert mc.rango_efectivo(sel('2026'), hoy) == ('01/01/2026', '15/11/2026')
    with pytest.raises(ValueError, match='todavía no empezó'):
        mc.rango_efectivo(sel('12/2026'), hoy)
    with pytest.raises(ValueError, match='365 días'):
        mc.rango_efectivo(sel('01/01/2025 a 02/01/2026'), hoy)
    with pytest.raises(ValueError, match='posterior a hoy'):
        sel('01/11/2026 a 16/11/2026')
    with pytest.raises(ValueError):
        sel('07/2026, 08/2026')


def test_progreso_por_sentido():
    assert mc.progress('mc:recibidos:descarga') == 'Recibidos · Esperando el archivo de ARCA…'
    assert mc.progress('mc:servicio') == 'Abriendo Mis Comprobantes…'
    assert mc.progress('mc:otros:descarga') is None


def test_teclado_tres_opciones_callback_corto_y_cancelar(monkeypatch):
    monkeypatch.setattr(mc, 'InlineKeyboardButton', lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data))
    monkeypatch.setattr(mc, 'InlineKeyboardMarkup', lambda rows: SimpleNamespace(inline_keyboard=rows))
    filas = mc.sentido_keyboard('a' * 10).inline_keyboard
    assert [b[0].text for b in filas[:-1]] == ['Emitidos', 'Recibidos', 'Emitidos y recibidos']
    assert [b[0].callback_data for b in filas[:-1]] == [f'pi:mcs:{"a"*10}:{k}' for k in ('emitidos', 'recibidos', 'ambos')]
    assert filas[-1][0].callback_data == f'pi:cancel:{"a"*10}'
    assert all(len(b[0].callback_data.encode()) <= 64 for b in filas)


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
    base = dict(nonce='a' * 10, stage='mc_sentido', nombre='Sintético', slug='sintetico', cuit='20000000001',
                contributor_id=7, mc_sentido=None, mc_desde=None, mc_hasta=None)
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize('sentido,aviso', [('emitidos', False), ('recibidos', True), ('ambos', True)])
def test_elegir_sentido_pide_periodo_y_avisa_la_fuente_de_compras(sentido, aviso):
    flow, s = Flow(), state()
    flow.states['k'] = s
    query = SimpleNamespace(answer=lambda *a, **k: asyncio.sleep(0))
    assert asyncio.run(mc.callback(flow, None, query, f'pi:mcs:{"a"*10}:{sentido}', 1, None, 'k'))
    assert (s.mc_sentido, s.stage) == (sentido, 'mc_period')
    assert ('Libro IVA presentado en Portal IVA' in flow.sent[-1][0]) is aviso
    assert '365 días' in flow.sent[-1][0]


def test_opcion_vencida_no_avanza():
    flow, s = Flow(), state(stage='mc_period')
    flow.states['k'] = s
    answers = []
    query = SimpleNamespace(answer=lambda text: answers.append(text) or asyncio.sleep(0))
    assert asyncio.run(mc.callback(flow, None, query, f'pi:mcs:{"a"*10}:ambos', 1, None, 'k'))
    assert s.mc_sentido is None and 'venció' in answers[0]
    assert not asyncio.run(mc.callback(flow, None, query, 'pi:mri:aaaaaaaaaa:iva', 1, None, 'k'))


def test_periodo_confirmado_fuera_del_tope_no_abre_arca():
    flow, s = Flow(), state(stage='mc_period', mc_sentido='emitidos')
    sel = parse_period('01/01/2025 a 02/01/2026', 'mis-comprobantes', today=date(2026, 11, 15))
    asyncio.run(mc.submit_period(flow, None, SimpleNamespace(chat_id=1, message_thread_id=None), s, 'k', sel))
    assert s.stage == 'mc_period' and s.mc_desde is None and '365 días' in flow.sent[-1][0]


def resultado(**kw):
    base = {'ok': True, 'operacion': 'arca_mis_comprobantes', 'slug': 'sintetico', 'cuit': '20000000001',
            'id_contribuyente': 7, 'escrituras_externas': False, 'escrituras_confirmadas': [], 'sentido': 'ambos',
            'desde': '01/07/2026', 'hasta': '31/07/2026',
            'consultas': [{'sentido': 'emitidos', 'etiqueta': 'Emitidos', 'estado': 'completado', 'cantidad_pantalla': 6757,
                           'archivo': {'ruta': '/c/comprobantes_consulta_csv_emitidos_1_20000000001_20261115-1256.zip', 'ruta_clientes': 'x', 'sha256': 'y'}},
                          {'sentido': 'recibidos', 'etiqueta': 'Recibidos', 'estado': 'cero', 'cantidad_pantalla': 0,
                           'motivo_texto': 'ARCA informó que no hay comprobantes para este filtro.'}],
            'xlsx': {'ruta': '/c/sintetico_mis_comprobantes_ambos_202607.xlsx', 'ruta_clientes': 'x', 'sha256': 'y'}}
    return {**base, **kw}


def test_resultado_tiene_que_ser_lo_pedido():
    s = state(mc_sentido='ambos', mc_desde='01/07/2026', mc_hasta='31/07/2026')
    consultas, excel, archivos, sin_excel = mc.validate_result(Flow(), s, resultado())
    assert excel.name == 'sintetico_mis_comprobantes_ambos_202607.xlsx' and len(archivos) == 1 and sin_excel is None
    for cambio, code in (({'cuit': '20000000002'}, 'NO_COINCIDE'), ({'hasta': '30/07/2026'}, 'NO_COINCIDE'),
                         ({'sentido': 'emitidos'}, 'NO_COINCIDE'), ({'xlsx': None}, 'EXCEL_NO_COINCIDE'),
                         ({'ok': False, 'motivo': 'LOGIN_CREDENCIALES_RECHAZADAS'}, 'LOGIN_CREDENCIALES')):
        with pytest.raises(RuntimeError, match=code):
            mc.validate_result(Flow(), s, resultado(**cambio))
    invertido = resultado(); invertido['consultas'] = list(reversed(invertido['consultas']))
    with pytest.raises(RuntimeError, match='CONSULTAS_NO_COINCIDEN'):
        mc.validate_result(Flow(), s, invertido)
    # Cero sólo con cantidad 0; un obtenido sin archivo no se acepta.
    malo = resultado(); malo['consultas'][1]['cantidad_pantalla'] = 3
    with pytest.raises(RuntimeError, match='CANTIDAD_INVALIDA'):
        mc.validate_result(Flow(), s, malo)
    sin_archivo = resultado(); sin_archivo['consultas'][0].pop('archivo')
    with pytest.raises(RuntimeError, match='ARCHIVO_FALTANTE'):
        mc.validate_result(Flow(), s, sin_archivo)
    # El ejecutor puede no entregar el Excel si dice por qué; los originales igual se entregan.
    _, excel, archivos, sin_excel = mc.validate_result(Flow(), s, resultado(xlsx=None, excel_motivo='MC_XLSX_NO_COINCIDE',
                                                                            excel_motivo_texto='El Excel no coincidió.'))
    assert excel is None and len(archivos) == 1 and sin_excel == 'El Excel no coincidió.'


class _Historial:
    def __init__(self):
        self.items = []; self.cerradas = 0

    async def start(self, **kw):
        return 'corrida'

    async def prepare_items(self, handle, refs):
        self.refs = refs
        return [{'id_item': i + 1} for i, _ in enumerate(refs)]

    async def attach_run(self, *a, **k):
        self.libro = k['libro']

    async def finish_item(self, handle, item, **kw):
        self.items.append((item, kw['state'], kw['reason_text']))

    async def close(self, handle):
        self.cerradas += 1


class _FlowCompleto(Flow):
    def __init__(self, tmp, result):
        super().__init__()
        self.history = _Historial(); self.result = result; self.tmp = tmp
        self.execution_locks = {}; self.processes = {}; self.clients_root = tmp; self.uv = Path('/usr/bin/uv')
        self.ediciones = []; self.consultas_sql = []

    def available(self):
        return True

    def _scope(self, operation=None):
        return SimpleNamespace(require_permission=lambda *a: None)

    def _by_id(self, item_id, telegram_id, operation=None):
        self.operacion_consultada = operation
        return [dict(self.binding)]

    def _query(self, sql):
        self.consultas_sql.append(sql)
        # Igual que _run_query: cada línea de psql ya llega decodificada.
        return [[["2026-07", "No está en Documentos; Recibidos queda como respaldo"]]]

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


def _corrida(monkeypatch, tmp_path, sentido, result, falla_envio=False):
    # Los archivos viven bajo la carpeta de clientes, como en producción.
    for registro in [result.get('xlsx'), *(c.get('archivo') for c in result.get('consultas', []))]:
        if registro:
            registro['ruta'] = str(tmp_path / Path(registro['ruta']).name)
    flow = _FlowCompleto(tmp_path, result)
    (tmp_path / 'mis_comprobantes.py').write_text('')
    flow.executor = tmp_path / 'portal_iva.py'
    flow.binding = {'id': 7, 'slug': 'sintetico', 'cuit': '20000000001', 'study_id': 1, 'relation_id': 2, 'relation_revision': 3,
                    'access_id': 4, 'representative_id': 7}
    s = state(mc_sentido=sentido, mc_desde='01/07/2026', mc_hasta='31/07/2026', stage='running', user_id='99',
              period='2026-07', scope_item=dict(flow.binding), cancelled=False, progress_message=object())
    enviados = []; comandos = []

    async def send_document(**kw):
        if falla_envio and kw['file_name'].endswith('.zip'):
            return SimpleNamespace(success=False, message_id=None)
        enviados.append(kw['file_name'])
        return SimpleNamespace(success=True, message_id='1', delivered_filename=kw['file_name'])

    async def proceso(*a, **k):
        comandos.append(a)
        return SimpleNamespace(returncode=0)

    async def verificada(*a, **k):
        return None
    monkeypatch.setattr(mc.asyncio, 'create_subprocess_exec', proceso)
    monkeypatch.setattr(mc, 'require_delivery_scope', verificada)
    monkeypatch.setattr(mc.shutil, 'rmtree', lambda *a, **k: None)
    asyncio.run(mc.run(flow, SimpleNamespace(send_document=send_document), 1, None, 'k', s))
    return flow, enviados, comandos


def test_ambos_entrega_excel_y_originales_y_resume_una_vez_con_la_fuente_de_compras(monkeypatch, tmp_path):
    flow, enviados, comandos = _corrida(monkeypatch, tmp_path, 'ambos', resultado())
    resumen = [t for t, _ in flow.sent if 'Emitidos: 6.757 comprobantes' in t]
    assert len(resumen) == 1
    assert 'Recibidos: ARCA informó que no hay comprobantes' in resumen[0]
    assert 'Libro IVA presentado en Portal IVA' in resumen[0]
    assert 'Libro IVA Compras 07/2026: No está en Documentos; Recibidos queda como respaldo' in resumen[0]
    assert flow.ediciones == ['Sintético · Mis Comprobantes Emitidos y recibidos: consulta terminada.']
    assert flow.history.refs == ['Mis Comprobantes Emitidos · 01/07/2026 a 31/07/2026', 'Mis Comprobantes Recibidos · 01/07/2026 a 31/07/2026']
    assert [e for _, e, _ in flow.history.items] == ['completado', 'completado'] and flow.history.libro == 'mis_comprobantes'
    assert enviados == ['sintetico_mis_comprobantes_ambos_202607.xlsx', 'comprobantes_consulta_csv_emitidos_1_20000000001_20261115-1256.zip']
    assert flow.operacion_consultada == 'mis-comprobantes'
    argv = list(comandos[0])
    assert argv[argv.index('--sentido') + 1] == 'ambos'
    assert argv[argv.index('--libros-iva') + 1] == '[["2026-07", "No está en Documentos; Recibidos queda como respaldo"]]'
    assert 'fn_mis_comprobantes_libros_iva_telegram(99,7,' in flow.consultas_sql[0]


def test_emitidos_no_consulta_libros_iva_ni_agrega_el_aviso(monkeypatch, tmp_path):
    r = resultado(sentido='emitidos', consultas=resultado()['consultas'][:1],
                  xlsx={'ruta': '/c/sintetico_mis_comprobantes_emitidos_202607.xlsx', 'ruta_clientes': 'x', 'sha256': 'y'})
    flow, enviados, comandos = _corrida(monkeypatch, tmp_path, 'emitidos', r)
    assert flow.consultas_sql == []
    assert not any('Portal IVA' in t for t, _ in flow.sent)
    argv = list(comandos[0])
    assert argv[argv.index('--libros-iva') + 1] == '[]'


def test_un_envio_fallido_no_pierde_lo_obtenido(monkeypatch, tmp_path):
    flow, enviados, _ = _corrida(monkeypatch, tmp_path, 'ambos', resultado(), falla_envio=True)
    assert enviados == ['sintetico_mis_comprobantes_ambos_202607.xlsx']
    resumen = [t for t, _ in flow.sent if 'Emitidos: 6.757 comprobantes' in t][0]
    assert 'No se pudo enviar por Telegram: comprobantes_consulta_csv_emitidos_1_20000000001_20261115-1256.zip' in resumen
    assert 'Documentos' in resumen
    assert [e for _, e, _ in flow.history.items] == ['completado', 'completado']


def test_clave_rechazada_se_detiene_sin_reintentar(monkeypatch, tmp_path):
    r = {'ok': False, 'operacion': 'arca_mis_comprobantes', 'motivo': 'LOGIN_CREDENCIALES_RECHAZADAS',
         'mensaje': 'ARCA rechazó la clave del acceso. No se reintentó.', 'escrituras_externas': False}
    flow, enviados, comandos = _corrida(monkeypatch, tmp_path, 'emitidos', r)
    assert len(comandos) == 1 and enviados == []
    assert 'ARCA rechazó la clave del acceso. No se reintentó.' in flow.sent[-1][0]
    assert [e for _, e, _ in flow.history.items] == ['fallido']


def test_libros_iva_sin_respuesta_no_abre_arca(monkeypatch, tmp_path):
    flow = _FlowCompleto(tmp_path, resultado())
    flow._query = lambda sql: []
    s = state(mc_sentido='recibidos', mc_desde='01/07/2026', mc_hasta='31/07/2026', user_id='99')
    with pytest.raises(RuntimeError, match='MC_LIBROS_IVA_NO_DISPONIBLE'):
        mc.libros_iva(flow, s)


def test_error_tecnico_dice_el_paso_la_corrida_y_una_sola_vez_que_no_se_modifico_arca(monkeypatch, tmp_path, caplog):
    """Corrida 51912 (contribuyente 158, Emitidos 2025): el texto repetía «No se modificó ARCA» y no decía qué pasó."""
    r = {'ok': False, 'operacion': 'arca_mis_comprobantes', 'motivo': 'MC_ERROR_TECNICO', 'escrituras_externas': False,
         'mensaje': 'ARCA no respondió como esperábamos en el paso «Emitidos · Poniendo el rango de fechas», así que no se '
                    'obtuvo ningún archivo. Probá de nuevo en un rato; si vuelve a pasar, avisá a soporte.',
         'diagnostico': {'paso': 'Emitidos · Poniendo el rango de fechas', 'tipo': 'ElementNotInteractableException',
                         'url': 'https://fes.afip.gob.ar/mcmp/jsp/setearContribuyente.do', 'carpeta': 'mis-comprobantes-x',
                         'ubicaciones': [{'archivo': 'mis_comprobantes.py', 'linea': 1, 'funcion': 'a_sentido'}]}}
    monkeypatch.setattr(_Historial, 'start', lambda self, **kw: asyncio.sleep(0, SimpleNamespace(run_id=51912)))
    with caplog.at_level('ERROR'):
        flow, enviados, _ = _corrida(monkeypatch, tmp_path, 'emitidos', r)
    texto = flow.sent[-1][0]
    assert texto == ('No se pudo completar Mis Comprobantes. ARCA no respondió como esperábamos en el paso «Emitidos · Poniendo '
                     'el rango de fechas», así que no se obtuvo ningún archivo. Probá de nuevo en un rato; si vuelve a pasar, '
                     'avisá a soporte con la corrida N.º 51912. No se modificó ARCA.')
    assert texto.count('No se modificó ARCA') == 1 and 'diagnóstico' not in texto
    assert enviados == [] and [e for _, e, _ in flow.history.items] == ['fallido']
    assert any('corrida=51912' in m and 'ElementNotInteractableException' in m for m in caplog.messages)


def test_mensaje_de_fallo_sin_texto_del_ejecutor():
    texto = mc.mensaje_fallo(None, None)
    assert texto.count('No se modificó ARCA') == 1 and 'avisá a soporte.' in texto
    assert mc.mensaje_fallo('ARCA rechazó la clave del acceso. No se reintentó.', 7).endswith('No se reintentó. No se modificó ARCA.')
