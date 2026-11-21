"""#192: mensajes del preflight compartido, sin ARCA ni credenciales."""
import pytest
from plugins.platforms.telegram.iva_configuracion import consulta,resumen,dividir,mensajes_lote
from plugins.platforms.telegram.preparar_lote_flow import PrepararLoteFlow

def test_ausencia_no_es_no():
    text=resumen(dict(faltantes=['modalidad de prorrateo']))
    assert 'Ficha › Impuestos › IVA' in text and 'No se iniciará ARCA' in text

def test_no_inventa_exportador_arca():
    text=resumen(dict(faltantes=[],valor=dict(prorrateo=True,modalidad='global',importador=True,tipo_importacion='bienes',exportador=False)))
    assert 'sí · global' in text and 'sí · bienes' in text and 'sin casilla en ARCA' in text

def test_sql_tipeado_y_periodo_interno():
    assert "date '2026-09-01'" in consulta(99,[1,2],'2026-09')
    with pytest.raises(ValueError):consulta(99,[1],'09/2026')

def test_no_pierde_motivo_configuracion():
    text='Completá Ficha › Impuestos › IVA: importador.'
    assert PrepararLoteFlow._no_incluidos(None,[dict(motivo=text)])[0]['motivo']==text

def test_lote_cien_compacto_completo_y_dentro_del_limite_telegram():
    names=[f'Cliente sintético {n:03d} 🧾' for n in range(100)]
    rows=[dict(nombre=n,configuracion=dict(faltantes=['prorrateo de IVA','importador','exportador'])) for n in names]
    chunks=mensajes_lote(rows,'09/2026')
    assert 1<len(chunks)<10
    for part in chunks:assert len(part.encode('utf-16-le'))//2<=3500
    for n in names:assert '\n'.join(chunks).count(n)==1
    text='No incluidos:\n'+'\n'.join(n+': Completá Ficha › Impuestos › IVA.' for n in names)
    parts=dividir(text)
    assert all(len(p.encode('utf-16-le'))//2<=3500 for p in parts)
    for n in names:assert '\n'.join(parts).count(n)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('rows,code',[
    ([[{'configuracion':{'faltantes':['importador']}}]],'configuracion_iva_incompleta'),
    ([], 'configuracion_iva_no_verificada'),
])
async def test_rechazo_preflight_registrado_sin_abrir_arca(rows,code):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow
    history=SimpleNamespace(reject=AsyncMock())
    flow=PortalIvaFlow(history=history);flow._by_id=lambda *args:[{'access_id':1}]
    flow._write_query=lambda *args:rows;flow._send=AsyncMock()
    key='sintetico';state=SimpleNamespace(operation='generar',user_id='990000192',contributor_id=1,nonce='abcdef0123')
    flow.states[key]=state
    await flow._submit_single_period(SimpleNamespace(),SimpleNamespace(chat_id=990000192),state,key,SimpleNamespace(months=['2026-09']))
    history.reject.assert_awaited_once()
    assert history.reject.await_args.kwargs['reason_code']==code
    assert history.reject.await_args.kwargs['operation']=='portal_iva_generar_csv'
    assert key not in flow.states and not flow.tasks


def test_por_defecto_se_informa_y_no_frena():
    # 0133 (contadora, 19/11 y 20/11/2026): sin respuesta vale «No», informado como por defecto.
    from plugins.platforms.telegram.iva_configuracion import version
    cfg=dict(id_dato=None,faltantes=[],por_defecto=['prorrateo','importador','exportador'],
             valor=dict(prorrateo=False,modalidad=None,importador=False,tipo_importacion=None,exportador=False))
    text=resumen(cfg)
    assert text.count('no (por defecto)')==3 and 'Completá' not in text
    assert version(cfg)==0
    assert version(dict(cfg,id_dato=7,por_defecto=['exportador']))==7
    for bad in (dict(cfg,por_defecto=['importador']),dict(cfg,id_dato=0),dict(cfg,id_dato='7')):
        with pytest.raises(ValueError):version(bad)
    with pytest.raises(ValueError):resumen(dict(cfg,por_defecto=['otro']))
    assert 'fn_iva_preflight_telegram_v2(' in consulta(99,[1],'2026-09')


def test_motivo_del_historial_en_una_linea_y_dentro_del_limite():
    # Corrida 59061: un salto de línea hizo que la base rechazara el cierre.
    from plugins.platforms.telegram.bot_run_history import texto_motivo
    assert texto_motivo('No se completó.\nQuedaron guardados en Documentos: a.csv, b.zip.')=='No se completó. Quedaron guardados en Documentos: a.csv, b.zip.'
    largo=texto_motivo('x '*400)
    assert len(largo)==300 and largo.endswith('…') and '\n' not in largo
