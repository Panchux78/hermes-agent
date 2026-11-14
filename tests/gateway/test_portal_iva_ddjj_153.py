import asyncio
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from plugins.platforms.telegram import portal_iva_ddjj_flow as ddjj
from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow, FlowState
from plugins.platforms.telegram.menu_buttons import ARCA_CONSULTAS, aligned_menu_label


def state():
    binding={'id':1,'nombre':'Sintético','slug':'sintetico','cuit':'20123456786','study_id':1,
             'relation_id':2,'relation_revision':1,'access_id':3,'representative_id':4}
    return FlowState(user_id='7',nonce='123456789a',stage='running',operation='ddjj-iva',
                     contributor_id=1,slug='sintetico',cuit='20123456786',nombre='Sintético',
                     period='2026-07',ddjj_periods=['2026-07','2026-08','2026-09'],scope_item=binding)


def payload():
    return {'ok':True,'operacion':ddjj.OPERATION,'slug':'sintetico','cuit':'20123456786','id_contribuyente':1,
            'representado_verificado':'20123456786','escrituras_externas':False,'escrituras_confirmadas':[],
            'periodos':[{'periodo':'2026-07','estado':'completado'},
                        {'periodo':'2026-08','estado':'no_presentado','motivo':'DDJJ_NO_PRESENTADA','motivo_texto':'Sin DDJJ presentada.'},
                        {'periodo':'2026-09','estado':'no_consultada','motivo':'DDJJ_LISTADO_INCOMPLETO','motivo_texto':'Listado incompleto.'}],
            'xlsx':{'ruta':'x.xlsx','ruta_clientes':'x.xlsx','sha256':'a'*64}}


def workbook(tmp_path):
    path=tmp_path/'x.xlsx'
    with zipfile.ZipFile(path,'w') as book:
        book.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="IVA"/><sheet name="Observaciones"/></sheets></workbook>')
    return path


def test_periodo_argentino_y_rango_acotado():
    assert ddjj.periods('09/2026')==['2026-09']
    assert len(ddjj.periods('01/2026 a 12/2026'))==12
    assert ddjj.periods('9/2026')==['2026-09']
    for bad in ('2026-09','202609','2026','13/2026','09/2026 a 08/2026','01/2023 a 01/2026'):
        with pytest.raises(ValueError):ddjj.periods(bad)


def test_comando_binding_y_sin_shell(tmp_path):
    for name in ('portal_iva.py','portal_iva_ddjj.py','uv'): (tmp_path/name).touch()
    flow=PortalIvaFlow(executor=tmp_path/'portal_iva.py',uv=tmp_path/'uv',clients_root=tmp_path,runtime_python=Path(sys.executable))
    cmd=ddjj.command(flow,state())
    assert cmd.count('--periodo')==3
    assert cmd[cmd.index('--id-acceso')+1]=='3'
    assert cmd[cmd.index('--id-titular')+1]=='4'
    assert '--operacion' not in cmd and '--captcha-stdin' in cmd


def test_comando_usa_el_python_fiscal_como_panel_sin_entorno_uv(tmp_path):
    for name in ('portal_iva.py','portal_iva_ddjj.py','uv'): (tmp_path/name).touch()
    flow=PortalIvaFlow(executor=tmp_path/'portal_iva.py',uv=tmp_path/'uv',clients_root=tmp_path,runtime_python=Path(sys.executable))
    cmd=ddjj.command(flow,state())
    assert cmd[:5]==['xvfb-run','-a',sys.executable,'-B',str(tmp_path/'portal_iva_ddjj.py')]
    assert str(tmp_path/'uv') not in cmd and '--with' not in cmd and 'python3' not in cmd


def test_sin_python_fiscal_no_abre_arca_ni_crea_entorno_alternativo(tmp_path):
    for name in ('portal_iva.py','portal_iva_ddjj.py','uv'): (tmp_path/name).touch()
    flow=PortalIvaFlow(executor=tmp_path/'portal_iva.py',uv=tmp_path/'uv',clients_root=tmp_path)
    with pytest.raises(RuntimeError,match='DDJJ_RUNTIME_NO_DISPONIBLE'):
        ddjj.command(flow,state())


def test_ejecutor_ausente_registra_rechazo(tmp_path):
    async def scenario():
        flow=PortalIvaFlow(executor=tmp_path/'portal_iva.py')
        flow._scope=lambda:NS(require_actor=Mock(),require_permission=Mock())
        flow._reject=AsyncMock();flow._send=AsyncMock()
        q=NS(answer=AsyncMock(),message=NS(message_id=1))
        await flow.start(None,q,'1',None,'7','ddjj-iva')
        assert not flow.states
        assert flow._reject.await_args.kwargs['operation']=='ddjj-iva'
    asyncio.run(scenario())


def test_menu_orden_y_progreso():
    rows=[r[2] for r in ARCA_CONSULTAS]
    assert rows.index('pi:ddjj')==rows.index('pi:lote')+1
    assert aligned_menu_label('arca_consultar','🧾','DDJJ de IVA a Excel').endswith('\u2800')
    assert 'percepciones aduaneras' in PortalIvaFlow._progress_text('ddjj:09/2026:aduaneras')
    assert ddjj.progress('ddjj:99/2026:aduaneras') is None
    assert 'Esperando para volver a ingresar' in ddjj.progress('ddjj:09/2026:reconexion')
    assert 'períodos pendientes' in PortalIvaFlow._progress_text('ddjj:09/2026:reconexion')


def test_valida_sujeto_periodos_y_unico_excel(tmp_path):
    flow=NS(_saved=Mock(return_value=workbook(tmp_path)))
    cases,path=ddjj.validate_result(flow,state(),payload())
    assert len(cases)==3 and path.suffix=='.xlsx'
    for override in ({'cuit':'20999999990'},{'escrituras_externas':True},{'periodos':[]},{'xlsx':None}):
        with pytest.raises(RuntimeError):ddjj.validate_result(flow,state(),{**payload(),**override})
    bad=tmp_path/'wrong.xlsx';bad.write_text('not a workbook');flow._saved.return_value=bad
    with pytest.raises(RuntimeError):ddjj.validate_result(flow,state(),payload())


@pytest.mark.parametrize('delivery_ok',[True,False])
def test_historial_archivo_antes_items_y_fallo_entrega(tmp_path,monkeypatch,delivery_ok):
    async def scenario():
        s=state(); calls=[]; path=workbook(tmp_path)
        history=NS(start=AsyncMock(return_value=NS(run_id=153)),prepare_items=AsyncMock(return_value=[{'id_item':i} for i in (1,2,3)]),
                   close=AsyncMock(),attach_run=AsyncMock(side_effect=lambda *a,**k:calls.append('attach')),
                   finish_item=AsyncMock(side_effect=lambda *a,**k:calls.append(k['state'])))
        flow=PortalIvaFlow(clients_root=tmp_path,history=history)
        flow._scope=lambda:NS(require_permission=Mock())
        flow._by_id=Mock(return_value=[s.scope_item]);flow._acquire_execution_lock=Mock();flow._release_execution_lock=Mock()
        flow._communicate_with_progress=AsyncMock(return_value=(json.dumps(payload()).encode(),b''))
        flow._saved=Mock(return_value=path);flow._stage_delivery_files=Mock(return_value=(None,[(path,3,'DDJJ')]))
        flow._edit_progress=AsyncMock();flow._send=AsyncMock()
        adapter=NS(send_document=AsyncMock(return_value=NS(success=delivery_ok,message_id='123' if delivery_ok else None)))
        monkeypatch.setattr(ddjj,'command',lambda *a:['synthetic'])
        monkeypatch.setattr(ddjj,'verify_representation',AsyncMock())
        monkeypatch.setattr(ddjj.asyncio,'create_subprocess_exec',AsyncMock(return_value=NS(returncode=0)))
        await ddjj.run(flow,adapter,'1',None,'key',s)
        assert calls[0]=='attach'
        assert calls[1:]==(['completado','rechazado','fallido'] if delivery_ok else ['fallido']*3)
        assert not flow.execution_locks
        assert flow._release_execution_lock.call_count==3
        assert history.attach_run.await_args.kwargs['libro']=='ddjj_iva'
        assert history.close.await_count==1
    asyncio.run(scenario())


def test_nada_completado_no_archivo_ni_ceros(tmp_path):
    data=payload()
    data['periodos'][0]={'periodo':'2026-07','estado':'fallido','motivo_texto':'No se pudo leer.'}
    data['xlsx']=None
    cases,path=ddjj.validate_result(NS(),state(),data)
    assert path is None and len(cases)==3


@pytest.mark.parametrize('failure',[asyncio.CancelledError, TimeoutError])
def test_cancelacion_timeout_terminan_proceso_y_liberan_locks(tmp_path,monkeypatch,failure):
    async def scenario():
        s=state(); events=[]; proc=NS(returncode=None)
        history=NS(start=AsyncMock(return_value=NS(run_id=153)),prepare_items=AsyncMock(return_value=[{'id_item':i} for i in (1,2,3)]),
                   close=AsyncMock(),attach_run=AsyncMock(),finish_item=AsyncMock(side_effect=lambda *a,**k:events.append(k['state'])))
        flow=PortalIvaFlow(clients_root=tmp_path,history=history)
        flow._scope=lambda:NS(require_permission=Mock())
        flow._by_id=Mock(return_value=[s.scope_item]);flow._acquire_execution_lock=Mock();flow._release_execution_lock=Mock()
        flow._communicate_with_progress=AsyncMock(side_effect=failure())
        flow._edit_progress=AsyncMock();flow._send=AsyncMock()
        async def terminate(p):
            assert p is proc
            events.append('terminated');p.returncode=-15
        flow._terminate_process=terminate
        monkeypatch.setattr(ddjj,'command',lambda *a:['synthetic'])
        monkeypatch.setattr(ddjj.asyncio,'create_subprocess_exec',AsyncMock(return_value=proc))
        adapter=NS(send_document=AsyncMock())
        if failure is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):await ddjj.run(flow,adapter,'1',None,'key',s)
        else:
            await ddjj.run(flow,adapter,'1',None,'key',s)
        assert events[0]=='terminated'
        assert events[1:]==(['cancelado']*3 if failure is asyncio.CancelledError else ['fallido']*3)
        assert not flow.execution_locks and not flow.processes
        assert flow._release_execution_lock.call_count==3
        adapter.send_document.assert_not_awaited()
        history.attach_run.assert_not_awaited()
        history.close.assert_awaited_once()
    asyncio.run(scenario())
