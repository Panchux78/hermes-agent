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
            with patch.object(f,'_query',side_effect=[[dict(telegram_id=990000002,id_consulta=7)],[row],[]]),patch.object(f,'_write',side_effect=[[row],[True]]) as write:
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
            # #240: un .xls no se rechaza en silencio; pide guardarlo como .xlsx y volver a mandarlo.
            for nombre in ('detalle octubre.xls','DETALLE.XLS'):
                msg=self.mensaje(nombre=nombre);self.assertTrue(await f.document(None,msg))
                self.assertEqual(msg.reply_text.await_args.args[0],'Ese archivo es .xls. Guardalo como .xlsx y volvé a mandarlo respondiendo al mensaje del Borrador.')
        enviar.assert_not_called()
        with patch.object(f,'_write',return_value=[dict(estado='sin_permiso')]):
            msg=self.mensaje();await f.document(None,msg)
            self.assertIn('no puede cargar',msg.reply_text.await_args.args[0])

    async def test_detalle_de_ventas_del_cliente_240(self):
        """#240: con reparto en la ficha, el Excel que responde al Borrador puede ser el detalle de ventas del cliente."""
        f=PadronActDomFlow();msg=self.mensaje(nombre='detalle octubre.xlsx')
        ok=dict(estado='aceptada',tipo='detalle_ventas',id_consulta=9,detalle=dict(comprobantes=7049,actividades={'293090':'16520100.36','453291':'170176915.20'}))
        with patch.object(f,'_write',return_value=[dict(estado='ok',ticket='2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11',id_consulta=7)]), \
             patch.object(f,'_enviar_devolucion',return_value=ok):
            self.assertTrue(await f.document(None,msg))
        texto=msg.reply_text.await_args.args[0]
        self.assertIn('Detalle de ventas recibido',texto);self.assertIn('7049 comprobantes',texto)
        self.assertIn('• 293090: $ 16.520.100,36',texto);self.assertIn('te llega por acá',texto)
        mal=dict(estado='rechazada',tipo='detalle_ventas',errores=[dict(hoja='VENTAS',fila=0,columna='tipoprecio',error='Falta la columna «tipoprecio» del formato que quedó en la ficha.')])
        msg=self.mensaje()
        with patch.object(f,'_write',return_value=[dict(estado='ok',ticket='2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11',id_consulta=7)]), \
             patch.object(f,'_enviar_devolucion',return_value=mal):
            await f.document(None,msg)
        texto=msg.reply_text.await_args.args[0]
        self.assertIn('No se tomó el detalle de ventas',texto);self.assertIn('VENTAS · tipoprecio',texto)

    async def test_entrega_tambien_el_excel_de_sifere_240(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'estudios/1/sintetica/sintetica-borrador-iva-iibb-2026-10-v01.xlsx';p.parent.mkdir(parents=True);p.write_bytes(b'borrador')
            s=root/'estudios/1/sintetica/sintetica-sifere-actividades-2026-10-v01.xlsx';s.write_bytes(b'sifere')
            a=dict(ruta=str(p.relative_to(root)),nombre=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            res=dict(papel='Borrador IVA e IIBB',compras_clasificadas=1,excepciones_iva=0,
                     reparto=dict(detalle=True,con_reparto=3,sin_reparto=0,totales={'293090':'400.00'}),
                     sifere=dict(listo=True,pendientes=[],ruta=str(s.relative_to(root)),nombre=s.name,sha256=hashlib.sha256(s.read_bytes()).hexdigest()))
            row=dict(nombre='Sintética',periodo='10/2026',estado='terminada',resultado=res,archivo=a)
            f=PadronActDomFlow(clients_root=root);adapter=SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True,message_id='12')),_bot=SimpleNamespace(send_message=AsyncMock()))
            with patch.object(f,'_query',side_effect=[[dict(telegram_id=990000002,id_consulta=7)],[row],[]]),patch.object(f,'_write',side_effect=[[row],[True]]) as write:
                await f.poll(adapter)
            nombres=[c.kwargs['file_name'] for c in adapter.send_document.await_args_list]
            self.assertEqual(nombres,[p.name,s.name])
            self.assertIn('3 ventas repartidas',adapter.send_document.await_args_list[0].kwargs['caption'])
            self.assertIn('no importó ni presentó nada',adapter.send_document.await_args_list[1].kwargs['caption'])
            self.assertIn("'enviada',12",write.call_args.args[0])  # el mensaje del Borrador sigue siendo el que se responde

    def test_el_borrador_con_reparto_avisa_lo_que_falta_240(self):
        row=dict(nombre='Sintética',periodo='10/2026',estado='terminada',progreso='',resultado=dict(papel='Borrador IVA e IIBB',
            reparto=dict(detalle=False),sifere=dict(listo=False,pendientes=['Falta cargar el detalle de ventas por actividad del período.'])))
        texto=resumen_padron(row)
        self.assertIn('falta el detalle de ventas del mes',texto);self.assertIn('adjuntando su Excel (.xlsx)',texto)
        self.assertLess(len(texto),1024)  # límite del pie de un documento en Telegram

    def test_el_pie_del_borrador_entra_en_telegram_240(self):
        largo='Dato faltante de una jurisdicción con un nombre largo y un motivo extenso que ocupa mucho lugar en el pie del documento '*2
        row=dict(nombre='Sintética',periodo='10/2026',estado='terminada',progreso='',resultado=dict(papel='Borrador IVA e IIBB',faltantes=[largo]*4,
            reparto=dict(detalle=True,con_reparto=7049,sin_reparto=2,totales={'293090':'16520100.36','453100':'64342534.05','453291':'170176911.95'}),
            sifere=dict(listo=False,pendientes=['2 ventas sin reparto ni actividad indicada (hoja EXCEPCIONES).'])))
        texto=resumen_padron(row)
        self.assertLessEqual(len(texto),1024)
        self.assertIn('7049 ventas repartidas',texto);self.assertIn('adjuntando su Excel (.xlsx)',texto)

    def test_el_borrador_dice_como_devolverlo(self):
        text=resumen_padron(dict(nombre='Sintética',periodo='10/2026',estado='terminada',resultado=dict(
            papel='Borrador IVA e IIBB',compras_clasificadas=1,excepciones_iva=0)))
        self.assertIn('respondé a este mensaje adjuntando el Excel',text)


class PadronExternoTelegram(IsolatedAsyncioTestCase):
    def mensaje(self):
        archivo=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'PK\x03\x04externo')))
        doc=SimpleNamespace(file_name='cuits.xlsx',file_size=1000,get_file=AsyncMock(return_value=archivo))
        return SimpleNamespace(chat_id=990000002,from_user=SimpleNamespace(id=990000002),document=doc,
                               reply_to_message=None,reply_text=AsyncMock())

    async def test_menu_externo_una_sola_cuenta_pide_excel(self):
        from plugins.platforms.telegram.padron_act_dom_flow import resumen_externo
        f=PadronActDomFlow();query=SimpleNamespace(answer=AsyncMock())
        adapter=SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock()))
        scope=SimpleNamespace(require_actor=lambda *_:None,require_permission=lambda *_:None)
        with patch.object(f,'_scope',return_value=scope),patch.object(f,'_write',return_value=[dict(id_estudio=1,nombre='Sintético')]):
            self.assertTrue(await f.callback(adapter,query,'pa:externo',990000002,None,990000002))
        self.assertIn('Adjuntá un Excel',adapter._bot.send_message.await_args.kwargs['text'])
        text=resumen_externo(dict(nombre='Sintético',estado='terminada',resultado=dict(filas=3,verificados=2,invalidos=1,sin_respuesta=0)))
        self.assertIn('3 filas',text);self.assertIn('Observaciones',text)

    async def test_excel_externo_usa_ticket_y_misma_vigilancia(self):
        f=PadronActDomFlow();msg=self.mensaje();state=State('990000002','nonce','excel_externo','externo');state.estudio_externo=1
        key=f._key(990000002,None,990000002);f.states[key]=state
        with patch.object(f,'_write',return_value=[{'ticket':'2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11'}]) as w, \
             patch.object(f,'_enviar_externo',return_value={'id_consulta':8}) as enviar, \
             patch.object(f,'_watch_padron',new_callable=AsyncMock):
            self.assertTrue(await f.document(None,msg))
            await f.watchers[8]
        self.assertIn("json_build_object('ticket',console.fn_padron_externo_ticket",w.call_args.args[0])
        self.assertEqual(enviar.call_args.args[0],'2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11')
        self.assertEqual(enviar.call_args.args[2],b'PK\x03\x04externo')
        self.assertNotIn(key,f.states)

    def test_ingreso_externo_rechaza_toda_url_no_loopback(self):
        f=PadronActDomFlow();ticket='2b0f6d1a-6c2e-4a43-9d5b-0a2f4f1c9e11'
        for url in ('https://127.0.0.1:8000','http://localhost:8000','http://127.0.0.1','http://127.0.0.1:8000/api',
                    'http://127.0.0.1:8000?destino=otro','http://usuario@127.0.0.1:8000'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                f.console_api_url=url;f._enviar_externo(ticket,'cuits.xlsx',b'PK')

    async def test_entrega_externa_reautoriza_y_conserva_archivo(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'estudios/1/externo.xlsx';p.parent.mkdir(parents=True);p.write_bytes(b'xlsx-externo')
            a=dict(ruta=str(p.relative_to(root)),nombre=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            row=dict(nombre='Sintético',periodo='Lista externa',estado='terminada',resultado=dict(filas=1,verificados=1),archivo=a)
            f=PadronActDomFlow(clients_root=root);adapter=SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True,message_id='13')),_bot=SimpleNamespace(send_message=AsyncMock()))
            with patch.object(f,'_query',side_effect=[[],[dict(telegram_id=990000002,id_consulta=8)],[row]]), \
                 patch.object(f,'_write',side_effect=[[row],[True]]) as write:
                await f.poll(adapter)
            self.assertEqual(adapter.send_document.await_args.kwargs['file_name'],p.name)
            self.assertIn('fn_padron_externo_entrega',write.call_args.args[0])
