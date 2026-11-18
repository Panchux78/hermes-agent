"""#194: período argentino, elección explícita y entregador único."""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase,TestCase
from unittest.mock import AsyncMock,Mock,patch
from plugins.platforms.telegram.sifere_actividades_flow import SifereActividadesFlow,resumen_actividades
from plugins.platforms.telegram.comarb_deducciones_flow import State
from plugins.platforms.telegram.period_input import parse_period

class Textos194(TestCase):
    def test_actividades_listas_no_ocultan_deducciones_incompletas(self):
        text=resumen_actividades(dict(estado='terminada',nombre_legal='Sintética',resultado=dict(
            operacion='sifere_actividades',listo=True,periodo='09/2026',ventas=2,jurisdicciones=1,
            advertencias=['Deducciones · Tucumán · percepciones: falta tipo de comprobante.'])))
        self.assertIn('Actividades listas',text);self.assertIn('falta tipo de comprobante',text)

    def test_no_promete_importable_con_pendientes(self):
        r=dict(estado='terminada',nombre_legal='Sintética',resultado=dict(operacion='sifere_actividades',listo=False,
            periodo='09/2026',ventas=2,jurisdicciones=1,pendientes=[dict(detalle='Falta asignar actividad.')]))
        text=resumen_actividades(r)
        self.assertIn('No hay archivo listo',text);self.assertIn('Falta asignar',text)
        self.assertNotIn('DDJJ presentadas',text)

class Flujo194(IsolatedAsyncioTestCase):
    async def test_varias_actividades_no_elige_la_primera_ni_encola(self):
        f=SifereActividadesFlow();s=State('990000002','nonce','periodo','uno',contributor={'id':1,'nombre':'Sintética'})
        key=f._key(990000002,None,'990000002');f.states[key]=s
        m=SimpleNamespace(reply_text=AsyncMock(),chat_id=990000002)
        with patch.object(f,'_query',return_value=[[{'codigo':'620100','descripcion':'Programación'},{'codigo':'620200','descripcion':'Consultoría'}]]),patch.object(f,'_write') as w:
            await f._submit_period(None,m,s,key,SimpleNamespace(visible=['09/2026']))
        w.assert_not_called();self.assertEqual(s.stage,'actividad')
        self.assertIn('Hay varias actividades',m.reply_text.await_args.args[0])

    async def test_una_actividad_se_asigna_sin_otro_trabajo_humano(self):
        f=SifereActividadesFlow();s=State('990000002','nonce','periodo','uno',contributor={'id':1,'nombre':'Sintética'})
        key=f._key(990000002,None,'990000002');m=SimpleNamespace(reply_text=AsyncMock())
        with patch.object(f,'_query',return_value=[[{'codigo':'620100','descripcion':'Programación'}]]),patch.object(f,'_encolar_actividad',new_callable=AsyncMock) as enqueue:
            await f._submit_period(None,m,s,key,SimpleNamespace(visible=['09/2026']))
        self.assertEqual(enqueue.await_args.args[4],'620100')
