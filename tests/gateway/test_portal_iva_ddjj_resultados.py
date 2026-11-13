"""DDJJ IVA: rango interanual y exclusiones explicadas sin fallar todo el pedido."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugins.platforms.telegram import portal_iva_ddjj_flow as ddjj


def unsupported(period='2025-05', form='731'):
    return {'periodo': period, 'estado': 'no_soportado',
            'presentacion': {'formulario': form}, 'motivo': 'DDJJ_FORMULARIO_NO_SOPORTADO',
            'motivo_texto': f'Presentada con F.{form}: esta consulta no lo transcribe'}


def test_rango_catorce_meses_argentino_sin_truncar():
    assert len(ddjj.periods('05/2025 a 06/2026')) == 14
    assert len(ddjj.periods('06/2025 a 06/2026')) == 13
    with pytest.raises(ValueError):
        ddjj.periods('01/2023 a 01/2026')


def test_todos_fuera_de_alcance_una_linea_no_tecnica():
    text = ddjj.summary_cases([unsupported(), unsupported('2025-04', '2002')])
    assert '\n' not in text
    assert 'F.731' in text and 'F.2002' in text and 'F.2051' in text
    assert 'No se generó un Excel' in text
    assert 'DDJJ_' not in text and 'fall' not in text.lower()


def test_mixto_cuenta_entregados_y_no_transcripto_con_motivo():
    cases = [unsupported(), *[{'periodo': p, 'estado': 'completado'}
              for p in ddjj.periods('06/2025 a 06/2026')]]
    text = ddjj.summary_cases(cases)
    assert '13 de 14' in text and '05/2025' in text and 'F.731' in text
    assert 'error técnico' not in text


def test_exclusion_no_se_registra_como_fallo_tecnico():
    async def scenario():
        history = SimpleNamespace(finish_item=AsyncMock(), close=AsyncMock())
        flow = SimpleNamespace(history=history, _history_reason_code=lambda c: c.lower())
        state = SimpleNamespace(nombre='Sintético', contributor_id=1)
        cases = [unsupported(), {'periodo': '2025-06', 'estado': 'completado'}]
        await ddjj.finish_cases(flow, state, cases, object(), {'2025-05': 1, '2025-06': 2}, set(), [])
        assert [c.kwargs['state'] for c in history.finish_item.await_args_list] == ['rechazado', 'completado']
        assert history.finish_item.await_args_list[0].kwargs['delivered_count'] == 0
        history.close.assert_awaited_once()
    asyncio.run(scenario())


def test_sin_f2051_no_se_acepta_archivo_y_se_conserva_cada_mes():
    cases = [unsupported()]
    state = SimpleNamespace(slug='sintetico', cuit='20123456786', contributor_id=1, ddjj_periods=['2025-05'])
    data = {'operacion': ddjj.OPERATION, 'slug': state.slug, 'cuit': state.cuit, 'id_contribuyente': 1,
            'escrituras_externas': False, 'escrituras_confirmadas': [], 'periodos': cases, 'xlsx': None}
    assert ddjj.validate_result(None, state, data) == (cases, None)
    with pytest.raises(RuntimeError):
        ddjj.validate_result(None, state, {**data, 'xlsx': {'ruta': 'inventada.xlsx'}})
    for presentation in ({}, {'formulario': '2051'}, {'formulario': 'desconocido'}):
        with pytest.raises(RuntimeError):
            ddjj.validate_result(None, state, {**data, 'periodos': [{**cases[0], 'presentacion': presentation}]})
