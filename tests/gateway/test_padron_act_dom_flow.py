"""#205: entrada común, mismos comprobantes y entrega del documento catalogado."""
from types import SimpleNamespace
from pathlib import Path
import hashlib
import tempfile
from unittest import IsolatedAsyncioTestCase,TestCase
from unittest.mock import AsyncMock,patch
from plugins.platforms.telegram.padron_act_dom_flow import PadronActDomFlow,resumen_padron
from plugins.platforms.telegram.comarb_deducciones_flow import State
from plugins.platforms.telegram.period_input import parse_period
from plugins.platforms.telegram.menu_buttons import ARCA_CONSULTAS

class ContratoPadron(TestCase):
    def test_menu_periodo_y_estado_sin_ausencias_inventadas(self):
        labels=[v[1] for v in ARCA_CONSULTAS]
        self.assertEqual(labels.index('Padrón ACT/Dom'),labels.index('Mis Comprobantes')+1)
        self.assertEqual(parse_period('09/2026','pa').visible,('09/2026',))
        with self.assertRaises(ValueError):parse_period('2026-09','pa')
        text=resumen_padron(dict(nombre='Sintética',periodo='09/2026',estado='terminada',resultado=dict(compras=20,ventas=10,excepciones=2)))
        self.assertIn('20 compras',text);self.assertIn('2 excepciones',text);self.assertNotIn('no clasificación fiscal',text)
        # #192 (pedido 3 de Pancho, 22/11/2026): el aviso cuenta compras clasificadas y excepciones de IVA, sin las de ventas.
        text=resumen_padron(dict(nombre='Sintética',periodo='10/2026',estado='terminada',resultado=dict(
            papel='Borrador IVA e IIBB',compras=71,compras_clasificadas=68,excepciones_iva=3,excepciones_ventas=548,excepciones=3)))
        self.assertTrue(text.startswith('Borrador IVA e IIBB · 10/2026 · Sintética'))
        self.assertIn('68 compras clasificadas y 3 excepciones para IVA',text);self.assertNotIn('548',text)

class FlujoPadron(IsolatedAsyncioTestCase):
    async def test_selector_ambiguo_no_salta_a_periodo(self):
        f=PadronActDomFlow();s=State('990000002','nonce','buscar','uno');key=f._key(990000002,None,s.user_id);f.states[key]=s
        msg=SimpleNamespace(text='sintetica',chat_id=990000002,from_user=SimpleNamespace(id=990000002),message_thread_id=None,reply_text=AsyncMock())
        with patch.object(f,'_query',return_value=[dict(id=1,nombre='Sintética uno',cuit='99000000001',slug='sintetica-uno'),dict(id=2,nombre='Sintética dos',cuit='99000000002',slug='sintetica-dos')]):
            await f.text(SimpleNamespace(),msg)
        self.assertEqual(s.stage,'buscar');self.assertIsNotNone(s.offer)
        self.assertIn('Elegí uno',msg.reply_text.await_args.args[0])

    async def test_submit_encola_mismo_periodo_sin_organismo(self):
        f=PadronActDomFlow();s=State('990000002','nonce','periodo','uno',contributor=dict(id=1,nombre='Sintética'))
        msg=SimpleNamespace(reply_text=AsyncMock());key=f._key(990000002,None,s.user_id)
        with patch.object(f,'_write',return_value=[{'id_consulta':7}]) as write,patch.object(f,'_watch_padron',new_callable=AsyncMock):
            await f._submit_period(None,msg,s,key,parse_period('09/2026','pa'))
            await f.watchers[7]
        self.assertIn("'pedir',NULL,1,'09/2026'",write.call_args.args[0])
        self.assertIn('Verificando Libros IVA',msg.reply_text.await_args.args[0])

    async def test_entrega_archivo_real_conserva_nombre_y_verifica_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'estudios/1/sintetica/sintetica-padron-act-dom-2026-09-v01.xlsx';p.parent.mkdir(parents=True);p.write_bytes(b'xlsx-sintetico')
            a=dict(ruta=str(p.relative_to(root)),nombre=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            row=dict(nombre='Sintética',periodo='09/2026',estado='terminada',resultado=dict(compras=1,ventas=2,excepciones=0),archivo=a)
            f=PadronActDomFlow(clients_root=root);adapter=SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True,message_id='12')),_bot=SimpleNamespace(send_message=AsyncMock()))
            with patch.object(f,'_query',side_effect=[[dict(telegram_id=990000002,id_consulta=7)],[row]]),patch.object(f,'_write',side_effect=[[row],[True]]) as write:
                await f.poll(adapter)
            self.assertEqual(adapter.send_document.await_args.kwargs['file_name'],p.name)
            self.assertIn("'enviada',12",write.call_args.args[0])

    async def test_entrega_no_sale_si_cambia_acceso(self):
        f=PadronActDomFlow();adapter=SimpleNamespace(send_document=AsyncMock(),_bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(f,'_query',return_value=[dict(telegram_id=990000002,id_consulta=7)]),patch.object(f,'_write',return_value=[None]):await f.poll(adapter)
        adapter.send_document.assert_not_awaited()


class DevolucionTelegram(IsolatedAsyncioTestCase):
    """#192: la contadora responde al mensaje del Borrador con el Excel corregido."""
    def mensaje(self,nombre='corregido.xlsx',size=1000,reply=55):
        archivo=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'PK\x03\x04datos')))
        doc=SimpleNamespace(file_name=nombre,file_size=size,get_file=AsyncMock(return_value=archivo))
        return SimpleNamespace(chat_id=990000002,from_user=SimpleNamespace(id=990000002),document=doc,
                               reply_to_message=SimpleNamespace(message_id=reply) if reply else None,reply_text=AsyncMock())

    async def test_otro_documento_sigue_de_largo(self):
        f=PadronActDomFlow()
        self.assertFalse(await f.document(None,self.mensaje(reply=None)))
        with patch.object(f,'_write',return_value=[None]):
            self.assertFalse(await f.document(None,self.mensaje()))

    async def test_excel_aceptado_y_borrador_nuevo_por_telegram(self):
        f=PadronActDomFlow();msg=self.mensaje()
        ok=dict(estado='aceptada',id_consulta=9,resumen={'COMPRAS':dict(correcciones=3,agregadas=2),'AJUSTES':dict(registrados=1)})
        with patch.object(f,'_write',return_value=[dict(estado='ok',ticket='2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11',id_consulta=7)]) as w, \
             patch.object(f,'_enviar_devolucion',return_value=ok) as enviar:
            self.assertTrue(await f.document(None,msg))
        self.assertIn('fn_papel_devolucion_ticket(990000002,55)',w.call_args.args[0])
        self.assertEqual(enviar.call_args.args[2],b'PK\x03\x04datos')
        texto=msg.reply_text.await_args.args[0]
        self.assertIn('3 correcciones',texto);self.assertIn('2 agregadas',texto);self.assertIn('1 ajustes globales',texto)
        self.assertIn('te llega por acá',texto)

    async def test_rechazo_dice_que_celdas_corregir(self):
        f=PadronActDomFlow();msg=self.mensaje()
        mal=dict(estado='rechazada',errores=[dict(hoja='COMPRAS',fila=12,columna='Corrección contador · clasificación',error='«x» no es una clasificación')])
        with patch.object(f,'_write',return_value=[dict(estado='ok',ticket='2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11',id_consulta=7)]), \
             patch.object(f,'_enviar_devolucion',return_value=mal):
            await f.document(None,msg)
        texto=msg.reply_text.await_args.args[0]
        self.assertIn('No se tomó ningún cambio',texto);self.assertIn('COMPRAS · fila 12',texto)

    async def test_no_xlsx_o_sin_permiso_no_se_envia(self):
        f=PadronActDomFlow()
        with patch.object(f,'_write',return_value=[dict(estado='ok',ticket='2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11',id_consulta=7)]), \
             patch.object(f,'_enviar_devolucion') as enviar:
            msg=self.mensaje(nombre='corregido.pdf');await f.document(None,msg)
            self.assertIn('Excel (.xlsx)',msg.reply_text.await_args.args[0])
        enviar.assert_not_called()
        with patch.object(f,'_write',return_value=[dict(estado='sin_permiso')]):
            msg=self.mensaje();await f.document(None,msg)
            self.assertIn('no puede cargar',msg.reply_text.await_args.args[0])

    def test_el_borrador_dice_como_devolverlo(self):
        text=resumen_padron(dict(nombre='Sintética',periodo='10/2026',estado='terminada',resultado=dict(
            papel='Borrador IVA e IIBB',compras_clasificadas=1,excepciones_iva=0)))
        self.assertIn('respondé a este mensaje adjuntando el Excel',text)
