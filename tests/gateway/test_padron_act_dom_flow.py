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
        self.assertIn('20 compras',text);self.assertIn('2 excepciones',text);self.assertIn('no clasificación fiscal',text)

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
