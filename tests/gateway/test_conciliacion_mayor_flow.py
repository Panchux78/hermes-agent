"""#102: interacción del bot con transporte falso; sin Telegram ni datos privados."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.platforms.telegram.conciliacion_mayor_flow import ConciliacionMayorFlow, State
from plugins.platforms.telegram.period_input import parse_period


def message(text=''):
    return SimpleNamespace(chat_id=55,message_thread_id=None,from_user=SimpleNamespace(id=12345),
                           text=text,reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))


def test_shared_contributor_picker_zero_one_many_and_period():
    async def run():
        flow=ConciliacionMayorFlow();key=flow._key(55,None,'12345')
        q=SimpleNamespace(answer=AsyncMock(),message=message())
        assert await flow.callback(None,q,'cm:start',55,None,'12345')
        candidates=[{'id':1,'nombre':'Prueba uno','slug':'prueba-uno','cuit':'99000000001'},
                    {'id':2,'nombre':'Prueba dos','slug':'prueba-dos','cuit':'99000000002'}]
        with patch.object(flow,'_search',return_value=candidates):await flow.text(None,message('prueba'))
        assert flow.states[key].stage=='search'
        s=flow.states[key]
        await flow.callback(None,q,f'cm:select:{s.nonce}:2',55,None,'12345')
        assert s.stage=='period' and s.candidate['id']==2
        await flow.text(None,message('09/2026'))
        assert s.period_candidate is not None and s.stage=='period'
        data=f'cm:period:yes:{s.period_candidate.nonce}'
        await flow.callback(None,q,data,55,None,'12345')
        assert s.stage=='account' and s.datos['desde']=='2026-09-01' and s.datos['hasta']=='2026-09-30'
    asyncio.run(run())


def test_cancel_running_requests_server_cancel_and_stops_poll():
    async def run():
        flow=ConciliacionMayorFlow();s=State('nonce','12345',stage='running',id=7)
        key=flow._key(55,None,'12345');flow.states[key]=s
        q=SimpleNamespace(answer=AsyncMock(),message=message())
        with patch.object(flow,'_request',return_value={}) as request:
            await flow.callback(None,q,'cm:cancel:nonce',55,None,'12345')
            request.assert_called_once_with('12345','cancelar',{'id':7})
        assert key not in flow.states
    asyncio.run(run())


def test_changing_menu_cancels_the_saved_draft_not_just_memory():
    async def run():
        flow=ConciliacionMayorFlow();s=State('nonce','12345',stage='ledger_upload',id=7)
        key=flow._key(55,None,'12345');flow.states[key]=s
        with patch.object(flow,'_request',return_value={}) as request:
            assert flow.cancel_pending(55,None,'12345')
            await asyncio.gather(*tuple(flow.cleanup_tasks))
            request.assert_called_once_with('12345','cancelar',{'id':7})
        assert key not in flow.states
    asyncio.run(run())


def test_upload_then_poll_delivers_official_result_and_no_old_file(tmp_path):
    async def run():
        flow=ConciliacionMayorFlow();s=State('nonce','12345',stage='ledger_upload',id=7,candidate={'nombre':'Sintético'})
        key=flow._key(55,None,'12345');flow.states[key]=s
        msg=message();msg.document=SimpleNamespace(file_name='mayor.csv',file_size=50,
            get_file=AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b'Fecha;Debe;Haber\n01/09/2026;0;100\n'))))
        output=tmp_path/'sintetico-conciliacion-v01.xlsx';output.write_bytes(b'PK resultado sintetico')
        adapter=SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
        def request(uid,action,datos):
            if action=='estado':return {'estado':'terminada','resultado':{'estado':'Con partidas a revisar','coincidencias':1,'pendientes':[]},'salida_relativa':'resultado.xlsx'}
            return {'estado':'pendiente'}
        with patch.object(flow,'_request',side_effect=request),patch('plugins.platforms.telegram.cruce_iva_flow.CruceIvaFlow._output_path',return_value=output):
            assert await flow.document(adapter,msg)
            await s.task
        assert adapter.send_document.await_count==1
        assert adapter.send_document.await_args.kwargs['file_name']==output.name
        assert key not in flow.states
    asyncio.run(run())


def test_mapping_sends_field_names_not_value_types():
    async def run():
        flow=ConciliacionMayorFlow();s=State('nonce','12345',stage='columns',id=1)
        s.columns=[{'indice':'0','encabezado':'Cuando','muestras':['01/09/2026','',''],'campo':'no_usar'}]
        s.mapping={'0':'no_usar'};s.preview={'hoja':'Mayor','hojas':['Mayor']}
        key=flow._key(55,None,'12345');flow.states[key]=s
        q=SimpleNamespace(answer=AsyncMock(),message=message())
        await flow.callback(None,q,'cm:column:0:nonce',55,None,'12345')
        assert s.stage=='field'
        await flow.callback(None,q,'cm:field:fecha:nonce',55,None,'12345')
        assert s.stage=='columns' and s.mapping=={'0':'fecha'}
    asyncio.run(run())


def test_diario_asks_bank_account_and_sends_it_with_mapping():
    # #102 caso real: un diario de cobranzas y pagos trae varias cuentas; se elige la del banco.
    async def run():
        flow=ConciliacionMayorFlow();s=State('nonce','12345',stage='columns',id=7)
        s.columns=[{'indice':'0','encabezado':'Saldo_Debe','muestras':['1,00','',''],'campo':'saldo_deudor'}]
        s.mapping={'0':'saldo_deudor'}
        s.preview={'hoja':'Hoja1','hojas':['Hoja1'],'cuentas':[{'codigo':'1110101','nombre':'CAJA','filas':3},
                                                             {'codigo':'1110202','nombre':'BCO PRUEBA','filas':2}]}
        key=flow._key(55,None,'12345');flow.states[key]=s
        q=SimpleNamespace(answer=AsyncMock(),message=message())
        sent=[]
        with patch.object(ConciliacionMayorFlow,'_request',side_effect=lambda uid,action,datos:sent.append((action,datos)) or {}), \
             patch.object(flow,'_start_poll'):
            await flow.callback(None,q,'cm:mapped:nonce',55,None,'12345')
            assert sent==[]  # sin cuenta elegida no se envía nada
            await flow.callback(None,q,'cm:account:5:nonce',55,None,'12345')
            assert 'cuenta_diario' not in s.datos
            await flow.callback(None,q,'cm:account:1:nonce',55,None,'12345')
            await flow.callback(None,q,'cm:mapped:nonce',55,None,'12345')
        assert sent==[('columnas',{'id':7,'opciones':{'mapeo':{'0':'saldo_deudor'},'hoja':'Hoja1','formato':'ar','cuenta_diario':'1110202'}})]
        shown='\n'.join(str(c.args[0]) for c in q.message.reply_text.call_args_list)
        assert 'Saldo deudor' in shown and 'Cuenta del banco en el diario: 1110202' in shown
    asyncio.run(run())


def test_transport_uses_stdin_not_argv_for_file():
    from plugins.platforms.telegram import conciliacion_mayor_flow as module
    data={'id':1,'contenido':base64.b64encode(b'sintetico').decode()}
    with patch.object(module.VencimientosFlow,'_connection_args',return_value=(['psql'],{})),patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"estado":"archivos"}')) as run:
        assert ConciliacionMayorFlow._request('12345','archivo',data)['estado']=='archivos'
    assert run.call_args.args[0]==['psql']
    assert 'fn_conciliacion_telegram' in run.call_args.kwargs['input']
    assert 'sintetico' not in str(run.call_args.args)


def test_period_uses_argentine_months_and_same_parser():
    p=parse_period('07/2026-09/2026','conciliacion-mayor')
    assert p.months==('2026-07','2026-08','2026-09')
    for raw in ('202609','2026-09','09/2026,10/2026'):
        try:parse_period(raw,'conciliacion-mayor')
        except ValueError:pass
        else:raise AssertionError('Aceptó otra sintaxis')
