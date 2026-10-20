import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from plugins.platforms.telegram.agip_deducciones_flow import AgipDeduccionesFlow
from plugins.platforms.telegram.comarb_deducciones_flow import State, resumen
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_menu_agip_mismo_orden_y_un_cliente_cartera_sin_login_nuevo(monkeypatch):
    import plugins.platforms.telegram.adapter as adapter_module
    import plugins.platforms.telegram.comarb_deducciones_flow as flow_module
    for module in (adapter_module,flow_module):
        monkeypatch.setattr(module,'InlineKeyboardButton',lambda text,callback_data:SimpleNamespace(text=text,callback_data=callback_data))
        monkeypatch.setattr(module,'InlineKeyboardMarkup',lambda rows:SimpleNamespace(inline_keyboard=rows))
    rows=TelegramAdapter._menu_panel_keyboard('agip_consultar').inline_keyboard
    assert [(r[0].text,r[0].callback_data) for r in rows[:2]]==[
        ('🧾  DDJJ de IIBB','ad:start'),('📑  Retenciones y Percepciones','om:agip_retenciones')]
    menu=TelegramAdapter._menu_panel_keyboard('agip_retenciones').inline_keyboard
    assert [r[0].callback_data for r in menu[:2]]==['ar:uno','ar:cartera']
    flow=AgipDeduccionesFlow()
    state=State('42','abc','periodo','uno',contributor={'id':7})
    assert flow._pedir_sql(42,state,'08/2026')=="SELECT console.fn_comarb_deducciones_pedir_telegram(42,7,'08/2026','agip_deducciones')::text;"
    state.modo='cartera'
    assert "'agip_deducciones'" in flow._pedir_sql(42,state,'08/2026')
    assert flow._period('2026') is None
    back=flow._back().inline_keyboard
    assert back[0][0].callback_data=='om:agip_consultar'


def test_callback_ajeno_no_consume_y_permiso_retirado_no_encola():
    async def run():
        flow=AgipDeduccionesFlow()
        adapter=SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()))
        query=SimpleNamespace(answer=AsyncMock())
        assert not await flow.callback(adapter,query,'cd:uno',42,None,'42')
        scope=SimpleNamespace(require_actor=lambda _:None,require_permission=lambda *a:(_ for _ in ()).throw(PermissionError()))
        with patch.object(flow,'_scope',return_value=scope),patch.object(flow,'_write') as write:
            assert await flow.callback(adapter,query,'ar:uno',42,None,'42')
            write.assert_not_called()
        assert not flow.states
    asyncio.run(run())


def test_resumen_agip_solo_informa_fuentes_realmente_consultadas():
    text=resumen({'periodo':'08/2026','nombre_legal':'Entidad sintética','estado':'terminada',
        'resultado':{'operacion':'agip_deducciones','estados':{'AGIP · Retenciones':{'estado':'sin_datos_verificado'},
        'AGIP · Percepciones':{'estado':'no_verificado','motivo':'No confirmó el período.'}}}})
    assert 'Retenciones y Percepciones' in text
    assert 'SIFERE' not in text and 'SIRCREB' not in text
    assert 'No confirmó el período' in text
    failed=resumen({'periodo':'08/2026','nombre_legal':'Entidad sintética','estado':'error',
        'motivo':'No se completó','resultado':{'operacion':'agip_deducciones'}})
    assert failed.startswith('📑 Retenciones y Percepciones') and 'Deducciones de IIBB' not in failed
