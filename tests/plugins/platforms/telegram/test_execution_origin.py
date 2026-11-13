"""Regression for #162: provenance is scoped to the actual inbound task."""
import asyncio,unittest
from types import SimpleNamespace as NS
from plugins.platforms.telegram.execution_origin import environment,incoming,programmed

class Origin162(unittest.IsolatedAsyncioTestCase):
    async def test_directo_es_prueba_y_handler_acredita_entrada(self):
        self.assertIn('contabot.canal=prueba',environment({})['PGOPTIONS'])
        seen=[]
        @incoming
        async def callback(update,ctx):seen.append(await asyncio.to_thread(environment,{}))
        await callback(NS(update_id=101,effective_message=NS(message_id=202,from_user=NS(id=303)),callback_query=None),None)
        self.assertIn('contabot.canal=telegram',seen[0]['PGOPTIONS'])
        self.assertEqual(seen[0]['CONTABOT_ORIGEN_ENTRADA'],'update:101/mensaje:202')
        self.assertEqual(seen[0]['CONTABOT_ORIGEN_ACTOR'],'303')
        self.assertIn('contabot.canal=prueba',environment({})['PGOPTIONS'])
    async def test_concurrencia_no_mezcla_cuentas_y_callback_es_interaccion(self):
        ready=asyncio.Event()
        arrived=0
        @incoming
        async def callback(update,ctx):
            nonlocal arrived
            arrived+=1
            if arrived==2:ready.set()
            await ready.wait()
            return environment({})
        a,b=await asyncio.gather(*[callback(NS(update_id=n,effective_message=NS(message_id=20,from_user=NS(id=999)),callback_query=NS(from_user=NS(id=n))),None) for n in [11,22]])
        self.assertEqual(a['CONTABOT_ORIGEN_ACTOR'],'11');self.assertEqual(b['CONTABOT_ORIGEN_ACTOR'],'22')
        self.assertIn('/callback',a['CONTABOT_ORIGEN_ENTRADA'])
    async def test_programado_no_se_disfraza_de_telegram(self):
        async def work():return environment({})
        env=await programmed(work(),'avisos_telegram')
        self.assertEqual(env['CONTABOT_ORIGEN_CANAL'],'automatico')
        self.assertEqual(env['CONTABOT_ORIGEN_PROCESO'],'avisos_telegram')
        self.assertEqual(environment({})['CONTABOT_ORIGEN_CANAL'],'prueba')
    async def test_referencia_no_admite_inyeccion_de_opciones(self):
        with self.assertRaises(ValueError):environment({'CONTABOT_PRUEBA_REFERENCIA':'x -c log_statement=all'})
