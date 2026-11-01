"""Entrega durable: transporte simulado; CAS real se prueba en el gate PostgreSQL."""
import asyncio
import hashlib
import re
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from plugins.platforms.telegram.comarb_ddjj_flow import ComarbPresentadasEntregas


class DeliveryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'estudios/1/sintetica.xlsx'
        self.path.parent.mkdir(parents=True);self.path.write_bytes(b'synthetic workbook transport')
        self.phase='reclamada';self.claimed=False;self.authorized=True;self.transitions=[]
        self.lock=threading.Lock()
        self.row=dict(id_lectura=7,id_contribuyente=1,telegram_id=42,autorizado=True,
            estado='terminada',nombre_legal='Sintética',periodos=['01/2026'],ruta='estudios/1/sintetica.xlsx',
            sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),resultado=dict(
                periodos_solicitados=['01/2026'],mensuales=[dict(periodo='01/2026',estado='sin_ddjj_verificada')]))
        def write(sql):
            with self.lock:
                if 'fn_comarb_ddjj_entregas_telegram()' in sql:return [self.row]
                if 'fn_comarb_ddjj_reclamar_telegram' in sql:
                    if self.claimed:return [None]
                    self.claimed=True;return [dict(fase=self.phase)]
                states=re.findall("'([^']*)'",sql)
                before,after=states[1:3]
                if before!=self.phase:return [False]
                self.phase=after;self.transitions.append(after);return [True]
        self.flow=SimpleNamespace(_write=write,_query=lambda sql:[{}] if self.authorized else [],clients_root=Path(self.tmp.name))
        self.deliver=ComarbPresentadasEntregas(self.flow)
        self.bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
                                 send_document=AsyncMock(return_value=SimpleNamespace(message_id=11)))

    async def test_dos_polls_solo_una_entrega_confirmada(self):
        await asyncio.gather(self.deliver.poll(self.bot),self.deliver.poll(self.bot))
        self.bot.send_message.assert_awaited_once();self.bot.send_document.assert_awaited_once()
        self.assertEqual(self.transitions,['enviando_aviso','aviso_enviado','enviando_archivo','entregada'])

    async def test_reinicio_despues_del_aviso_no_lo_repite(self):
        self.phase='aviso_enviado'
        await self.deliver.poll(self.bot)
        self.bot.send_message.assert_not_awaited();self.bot.send_document.assert_awaited_once()
        self.assertEqual(self.phase,'entregada')

    async def test_envio_ambiguo_no_reenvia_excel_ni_repite_sifere(self):
        self.phase='notificando_error'
        await self.deliver.poll(self.bot)
        self.bot.send_document.assert_not_awaited();self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.phase,'error_notificado')

    async def test_revocacion_entre_aviso_y_excel(self):
        async def notice(**kwargs):
            self.authorized=False
            return SimpleNamespace(message_id=10)
        self.bot.send_message.side_effect=notice
        await self.deliver.poll(self.bot)
        self.bot.send_document.assert_not_awaited();self.assertEqual(self.phase,'sin_permiso')

    async def test_corte_durante_envio_conserva_fase_ambigua(self):
        self.bot.send_document.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):await self.deliver.poll(self.bot)
        self.assertEqual(self.phase,'enviando_archivo')
        self.assertNotIn('entregada',self.transitions)

    async def test_fallo_del_transporte_no_marca_entregado(self):
        self.bot.send_document.side_effect=RuntimeError('synthetic network failure')
        await self.deliver.poll(self.bot)
        self.assertEqual(self.phase,'error');self.assertNotIn('entregada',self.transitions)
