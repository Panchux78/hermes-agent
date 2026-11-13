"""DDJJ presentadas: navegación y entrada determinista. Sólo datos sintéticos."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_menu_presentadas_y_dos_modalidades(monkeypatch):
    from tests.gateway.test_comarb_deducciones_flow import _menu
    _,menu=_menu(monkeypatch,'comarb_consultar')
    assert 'om:comarb_presentadas' in [row[0]['callback_data'] for row in menu[:-1]]
    title,children=_menu(monkeypatch,'comarb_presentadas')
    assert [row[0]['callback_data'] for row in children[:-1]]==['dj:periodo','dj:lote']
    assert [row[0]['text'].rstrip('\u2800\u200a') for row in children[:-1]]==['📅  Consulta por período','📦  Consulta por lote']
    assert children[-1][0]['callback_data']=='om:comarb_consultar'
    assert children[-1][1]['callback_data']=='om:close'
    assert 'Sólo lectura' in title


class PresentadasFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_rutea_callback_sin_llm_y_rechaza_grupos(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from plugins.platforms.telegram.adapter import TelegramAdapter
        self.assertTrue(callable(getattr(TelegramAdapter,'_handle_comarb_presentadas_callback',None)),
                        'Falta el despacho autorizado del callback de los botones')
        adapter=object.__new__(TelegramAdapter)
        adapter._callback_authorized=AsyncMock(return_value=True)
        flow=SimpleNamespace(callback=AsyncMock())
        adapter._comarb_presentadas_flow=flow
        query=SimpleNamespace(data='dj:lote',from_user=SimpleNamespace(id=42),answer=AsyncMock())
        cb={'chat_id':42,'thread_id':None,'chat_type':'private'}
        self.assertTrue(await adapter._handle_comarb_presentadas_callback(query,'dj:lote',cb))
        flow.callback.assert_awaited_once_with(adapter,query,'dj:lote',42,None,'42')
        cb['chat_type']='group'
        self.assertTrue(await adapter._handle_comarb_presentadas_callback(query,'dj:lote',cb))
        self.assertEqual(flow.callback.await_count,1)
        self.assertFalse(await adapter._handle_comarb_presentadas_callback(query,'ad:start',cb))

    async def test_no_despacha_hasta_validar_cliente_y_periodos(self):
        import importlib.util
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        self.assertIsNotNone(importlib.util.find_spec('plugins.platforms.telegram.comarb_ddjj_flow'),
                             'Falta conectar los botones con el flujo fiscal')
        from plugins.platforms.telegram.comarb_ddjj_flow import ComarbPresentadasFlow
        row = {'id':7,'nombre':'Empresa sintética SA','cuit':'30000000011'}
        class Scope:
            def require_actor(self, user): pass
            def require_permission(self, user, permission): pass
            def search(self, user, term): return [row]
            def by_id(self, user, cid): return [row] if cid == 7 else []
        flow = ComarbPresentadasFlow(query_connection=lambda:None, write_connection=lambda:None)
        flow._scope = lambda:Scope()
        sql = []
        def write(query):
            sql.append(query)
            return [{'estado':'encolada','id_lectura':9,'periodos':['01/2025','08/2025']}]
        flow._write = write
        flow._watch = AsyncMock()
        adapter = SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()))
        query = SimpleNamespace(data='dj:lote',from_user=SimpleNamespace(id=42),answer=AsyncMock())
        def msg(text):
            return SimpleNamespace(text=text,chat_id=42,message_thread_id=None,from_user=SimpleNamespace(id=42),
                reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock(),reply_markup=None)))
        await flow.callback(adapter,query,'dj:lote',42,None,'42')
        self.assertEqual(sql,[])
        await flow.text(adapter,msg('empresa sintética'))
        self.assertEqual(sql,[])
        await flow.text(adapter,msg('01/20125 hasta 04/2025'))
        self.assertEqual(sql,[])
        self.assertTrue(flow.states)
        await flow.text(adapter,msg('01/2025, 08/2025'))
        self.assertEqual(sql,[])
        from tests.gateway.period_helpers import confirm_period
        await confirm_period(flow,adapter,42,'42')
        self.assertEqual(len(sql),1)
        self.assertIn('fn_comarb_ddjj_pedir_telegram(42,7,',sql[0])
        self.assertIn(json.dumps(['01/2025','08/2025'],separators=(',',':')),sql[0])
        self.assertEqual(flow.states,{})
        self.assertNotIn(row['cuit'],sql[0])
        await __import__('asyncio').sleep(0)
        self.assertEqual(flow._watch.await_count,1)


if __name__ == '__main__':
    unittest.main()
