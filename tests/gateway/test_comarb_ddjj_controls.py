"""Regresión de controles DDJJ: datos sintéticos, sin token, base ni portales."""
import asyncio
from datetime import date
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.comarb_ddjj_flow import ComarbPresentadasFlow, resumen_presentadas
from plugins.platforms.telegram.comarb_ddjj_periodos import parse_periodos

ROW={'id':7,'nombre':'Empresa sintética SA','cuit':'30000000011'}

class Scope:
    def __init__(self,rows):self.rows=rows
    def require_actor(self,user):pass
    def require_permission(self,user,permission):pass
    def search(self,user,term):return self.rows
    def by_id(self,user,cid):return [r for r in self.rows if r['id']==cid]

class Flow(ComarbPresentadasFlow):
    def __init__(self):
        super().__init__(query_connection=lambda:None,write_connection=lambda:None)
        self.scope=Scope([ROW]);self.sql=[]
        self._watch=AsyncMock()
    def _scope(self):return self.scope
    def _write(self,query):
        self.sql.append(query)
        return [{'estado':'encolada','id_lectura':9}]

def message(text):
    return SimpleNamespace(text=text,chat_id=42,chat=SimpleNamespace(type='private'),message_thread_id=None,
        from_user=SimpleNamespace(id=42),reply_text=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock(),reply_markup=None)))

def query(data):
    return SimpleNamespace(data=data,from_user=SimpleNamespace(id=42),answer=AsyncMock())

class PeriodGrammarRegression(unittest.TestCase):
    def test_formatos_y_limites_no_se_reparan(self):
        for text,mode in (('1/2025','periodo'),('01/20125','periodo'),('00/2025','periodo'),
            ('13/2025','periodo'),('01-2025','periodo'),('01/2025 - 01/2025','lote'),
            ('08/2025 hasta 01/2025','lote'),('01/2025 hasta 04/2025 - 08/2025','lote'),
            ('01/2025','lote'),('10/2026','periodo'),('11/2026','periodo'),('01/2009','periodo')):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):parse_periodos(text,mode,today=date(2026,10,28))
    def test_orden_aislado_y_rango_inclusivo(self):
        self.assertEqual(parse_periodos('08/2025 - 01/2025 - 03/2025','lote',today=date(2026,10,28)),['08/2025','01/2025','03/2025'])
        self.assertEqual(parse_periodos('01/2025 hasta 04/2025','lote',today=date(2026,10,28)),['01/2025','02/2025','03/2025','04/2025'])
        self.assertEqual(parse_periodos('01/2025','periodo',today=date(2026,10,28)),['01/2025'])

class StateRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=Flow();self.adapter=SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()))
    async def asyncTearDown(self):await self.flow.close()
    async def start(self,mode='lote'):
        q=query('dj:'+mode)
        await self.flow.callback(self.adapter,q,q.data,42,None,'42')
        return self.flow.states['42::42']
    async def test_periodo_unico_no_agrega_confirmacion(self):
        await self.start('periodo');await self.flow.text(self.adapter,message('sintética'))
        await self.flow.text(self.adapter,message('09/2026'))
        self.assertEqual(len(self.flow.sql),1);self.assertIn('["09/2026"]',self.flow.sql[0])
    async def test_rango_encola_un_trabajo_con_lista_completa(self):
        await self.start();await self.flow.text(self.adapter,message('sintética'))
        await self.flow.text(self.adapter,message('01/2025 hasta 04/2025'))
        self.assertEqual(len(self.flow.sql),1)
        self.assertIn('["01/2025","02/2025","03/2025","04/2025"]',self.flow.sql[0])
    async def test_vencimiento_no_despacha(self):
        state=await self.start();state.created_at-=601
        msg=message('sintética');await self.flow.text(self.adapter,msg)
        self.assertEqual(self.flow.states,{});self.assertEqual(self.flow.sql,[])
        self.assertIn('venció',msg.reply_text.await_args.args[0])
    async def test_cancelacion_vieja_no_borra_pedido_nuevo(self):
        state=await self.start();old_nonce=state.nonce
        current=await self.start();q=query('dj:cancel:'+old_nonce)
        await self.flow.callback(self.adapter,q,q.data,42,None,'42')
        self.assertEqual(self.flow.states['42::42'].nonce,current.nonce)
        q=query('dj:cancel:'+current.nonce)
        await self.flow.callback(self.adapter,q,q.data,42,None,'42')
        self.assertFalse(self.flow.states);self.assertEqual(self.flow.sql,[])
    async def test_seleccion_revocada_no_encola(self):
        self.flow.scope.rows=[ROW,{**ROW,'id':8,'nombre':'Otra sintética'}]
        state=await self.start();await self.flow.text(self.adapter,message('sintética'))
        self.assertIsNotNone(state.offer)
        self.flow.scope.rows=[]
        q=query('dj:select:'+state.nonce+':7')
        await self.flow.callback(self.adapter,q,q.data,42,None,'42')
        self.assertEqual(self.flow.states,{});self.assertEqual(self.flow.sql,[])
    async def test_teardown_cancela_seguimiento_no_proceso_fiscal(self):
        await self.start()
        task=asyncio.create_task(asyncio.Event().wait());self.flow.watchers[9]=task
        await self.flow.close()
        self.assertTrue(task.cancelled());self.assertEqual(self.flow.watchers,{})
        self.assertEqual(self.flow.states,{})
    async def test_adapter_real_no_emite_evento_llm(self):
        adapter=object.__new__(TelegramAdapter)
        adapter._accept_update=Mock();adapter._callback_ctx=Mock(return_value={'chat_id':42,'thread_id':None,'chat_type':'private'})
        adapter._callback_authorized=AsyncMock(return_value=True)
        adapter._comarb_presentadas_flow=self.flow
        adapter._bot=self.adapter._bot
        adapter._enqueue_text_event=Mock()
        q=query('dj:periodo')
        await adapter._handle_callback_query(SimpleNamespace(callback_query=q),SimpleNamespace())
        self.assertTrue(self.flow.states)
        adapter._is_user_authorized_from_message=Mock(return_value=True)
        adapter._panel_captcha_bridge=None
        msg=message('sintética')
        await adapter._handle_text_message(SimpleNamespace(effective_message=msg),SimpleNamespace(bot=None))
        self.assertEqual(self.flow.states['42::42'].stage,'periodo')
        adapter._enqueue_text_event.assert_not_called()
    async def test_otro_boton_no_puede_consumir_campos_del_pedido_activo(self):
        state=await self.start()
        adapter=object.__new__(TelegramAdapter)
        adapter._comarb_presentadas_flow=self.flow
        adapter._callback_authorized=AsyncMock(return_value=True)
        q=query('pi:descargar')
        self.assertTrue(await adapter._handle_comarb_presentadas_callback(q,q.data,
            {'chat_id':42,'thread_id':None,'chat_type':'private'}))
        self.assertEqual(self.flow.states['42::42'].nonce,state.nonce)
        self.assertIn('cancelá',q.answer.await_args.kwargs['text'])

    async def test_adapter_rechaza_usuario_no_autorizado(self):
        adapter=object.__new__(TelegramAdapter)
        adapter._callback_authorized=AsyncMock(return_value=False)
        flow=SimpleNamespace(callback=AsyncMock());adapter._comarb_presentadas_flow=flow
        self.assertTrue(await adapter._handle_comarb_presentadas_callback(query('dj:lote'),'dj:lote',{'chat_id':42,'thread_id':None,'chat_type':'private'}))
        flow.callback.assert_not_called()

class DeliverySummaryRegression(unittest.TestCase):
    def test_faltantes_no_se_anuncian_como_completo(self):
        with self.assertRaises(ValueError):
            resumen_presentadas({'estado':'terminada','nombre_legal':'Sintética','periodos':['01/2025','08/2025'],
                'resultado':{'periodos_solicitados':['01/2025','08/2025'],'mensuales':[{'periodo':'01/2025','estado':'sin_ddjj_verificada'}]}})
    def test_ausencia_y_error_no_inventan_importes(self):
        text=resumen_presentadas({'estado':'terminada','nombre_legal':'Sintética','periodos':['01/2025'],
            'resultado':{'periodos_solicitados':['01/2025'],'mensuales':[{'periodo':'01/2025','estado':'sin_ddjj_verificada'}]}})
        self.assertIn('sin DDJJ presentada, verificado',text);self.assertNotIn('$',text)
        self.assertIn('No se entregó un lote completo',resumen_presentadas({'estado':'error','motivo':'Fallo sintético'}))

if __name__=='__main__':unittest.main()
