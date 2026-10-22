"""Sólo incidencias agrupadas del calendario al estudio, nunca a clientes."""
import asyncio
import logging

logger=logging.getLogger(__name__)


class VencimientosAvisos:
    def __init__(self,query):
        self.query=query

    async def poll(self,bot):
        rows=await asyncio.to_thread(self.query,
            "SELECT row_to_json(x)::text FROM console.fn_vencimientos_avisos_reclamar('telegram') x;")
        for row in rows:
            ok=False
            try:
                await bot.send_message(chat_id=int(row['telegram_id']),text=row['mensaje'])
                ok=True
            except Exception:
                logger.warning('[VENCIMIENTOS] aviso no entregado; registrado para revisión')
            ids=','.join(str(int(x)) for x in row['ids'])
            await asyncio.to_thread(self.query,
                f"SELECT console.fn_vencimientos_avisos_confirmar({int(row['id_usuario'])},ARRAY[{ids}]::bigint[],'telegram',{'true' if ok else 'false'})::text;")

    async def run(self,adapter):
        while True:
            try:
                if adapter._bot is not None:await self.poll(adapter._bot)
            except asyncio.CancelledError:raise
            except Exception:logger.warning('[VENCIMIENTOS] no se pudieron consultar los avisos pendientes')
            await asyncio.sleep(30)
