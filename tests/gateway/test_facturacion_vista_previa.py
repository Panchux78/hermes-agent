import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock
from plugins.platforms.telegram.execution_origin import incoming

spec = spec_from_file_location('facturacion_flow_candidato', Path(__file__).resolve().parents[2] /
                               'plugins/platforms/telegram/facturacion_flow.py')
flow = module_from_spec(spec)
spec.loader.exec_module(flow)


def test_no_encola_hasta_confirmar_vista_previa_y_nonce_no_se_repite(monkeypatch):
    asyncio.run(_probar(monkeypatch))


async def _probar(monkeypatch):
    bot = flow.FacturacionFlow(lambda: None)
    bloque = {"comprobantes": [{"estado": "por_verificar", "neto":"100.00","iva":"21.00","total":"121.00"}],
              "observaciones": [], "emitidos": 0}
    candidatos = [(dict(fila=4,total="121.00"), {})]
    calls = []
    def solicitar(resultado, lista, encolar):
        assert resultado is bloque and lista is candidatos
        return encolar([{"fila":4}])["id_pedido"]
    fake = SimpleNamespace(revisar=lambda *a,**k: (bloque,candidatos), control_excel=lambda _: b'x',
                           MAX_PREVIA=30, solicitar_previa=solicitar)
    monkeypatch.setattr(flow, "cargar",lambda _:fake)
    monkeypatch.setattr(bot, "_actor",lambda user:int(user))
    monkeypatch.setattr(bot, "_job",lambda actor,job,accion,data,*,origen_mensaje:
                        calls.append((actor,accion,data,origen_mensaje)) or {"id_pedido":7})
    monkeypatch.setattr(bot, "_wait_job",AsyncMock())
    message = SimpleNamespace(chat_id=42,message_id=31,from_user=SimpleNamespace(id=42),reply_document=AsyncMock(),
                             reply_text=AsyncMock())
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'file')))
    message.document=SimpleNamespace(file_name='test.xlsx',file_size=4,get_file=AsyncMock(return_value=file))
    query=SimpleNamespace(from_user=message.from_user,message=message,answer=AsyncMock(),edit_message_text=AsyncMock())
    metadata={"chat_id":"42","chat_type":"private"}
    await bot.callback(query,"fe:start",metadata)
    upload=SimpleNamespace(update_id=100,effective_message=message,callback_query=None,effective_chat=None)
    async def received(update, context):
        return await bot.document(update.effective_message)
    assert await incoming(received)(upload,None)
    assert calls==[]
    sent=message.reply_document.await_args.kwargs
    assert "Neto, IVA y total" in sent["caption"]
    previews = bot.previews.get(("42","42"))
    assert previews is not None
    confirmation=f"fe:confirm:{previews[0]}"
    assert sent["reply_markup"] is not None
    await bot.callback(query,"fe:confirm:incorrecto",metadata)
    assert calls==[]
    await bot.callback(query,confirmation,metadata)
    await bot.jobs[("42","42")][1]
    await bot.callback(query,confirmation,metadata)
    assert calls==[(42,"crear",{"casos":[{"fila":4}]},("telegram","","update:100/mensaje:31","42"))]


def test_emitir_solo_desde_el_boton_privado_recibido(monkeypatch):
    asyncio.run(_emitir(monkeypatch))


async def _emitir(monkeypatch):
    bot=flow.FacturacionFlow(lambda:None)
    calls=[]
    monkeypatch.setattr(flow,'cargar',lambda _:object())
    monkeypatch.setattr(bot,'_actor',lambda actor:int(actor))
    monkeypatch.setattr(bot,'_emision',lambda *args,**kwargs:calls.append((args,kwargs)) or {'id_emision':101})
    monkeypatch.setattr(bot,'_wait_job',AsyncMock())
    actor=SimpleNamespace(id=42)
    msg=SimpleNamespace(message_id=31)
    action='fe:emit:77:nonce_ficticio'
    query=SimpleNamespace(from_user=actor,message=msg,answer=AsyncMock(),data=action)
    bot.confirmaciones[('42','42')]=(77,'nonce_ficticio',monotonic()+60,[4,7],31)
    await bot.callback(query,action,{'chat_id':'42','chat_type':'private'})
    await bot.callback(query,action,{'chat_id':'99','chat_type':'group'})
    assert not calls
    @incoming
    async def callback(update,_):
        await bot.callback(query,action,{'chat_id':'42','chat_type':'private'})
    update=SimpleNamespace(update_id=101,effective_message=msg,effective_chat=actor,callback_query=query)
    await callback(update,None)
    await bot.jobs[('42','42')][1]
    await callback(update,None)
    assert calls==[((42,77,'emitir',{'confirmo':True,'filas':[4,7]}),
                    {'origin':('telegram','','update:101/mensaje:31','42')})]
