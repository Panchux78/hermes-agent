import unittest
from unittest.mock import AsyncMock
from plugins.platforms.telegram.vencimientos_avisos import VencimientosAvisos


class Avisos125(unittest.IsolatedAsyncioTestCase):
    async def test_entrega_agrupada_y_confirmacion_sin_reenvio(self):
        queries=[]
        def query(sql):
            queries.append(sql)
            return [dict(telegram_id=123,id_usuario=7,ids=[1,2],mensaje='Novedades del estudio')] if 'reclamar' in sql else [True]
        bot=AsyncMock()
        await VencimientosAvisos(query).poll(bot)
        bot.send_message.assert_awaited_once_with(chat_id=123,text='Novedades del estudio')
        self.assertIn("ARRAY[1,2]::bigint[],'telegram',true",queries[-1])

    async def test_fallo_entrega_se_registra_sin_exponer_excepcion(self):
        queries=[]
        def query(sql):
            queries.append(sql)
            return [dict(telegram_id=123,id_usuario=7,ids=[1],mensaje='Novedad')] if 'reclamar' in sql else [True]
        bot=AsyncMock();bot.send_message.side_effect=RuntimeError('No debe salir al usuario')
        await VencimientosAvisos(query).poll(bot)
        self.assertIn("'telegram',false",queries[-1])


if __name__=='__main__':unittest.main()
