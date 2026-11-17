"""Regresión #193: mismo entregador y permisos vivos para los TXT de SIFERE."""
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesEntregas


def scenario(tmp_path, *, revocar=False):
    relative='estudios/1/sintetico/2026/09/comarb/cliente-importar-sifere-901-2026-09.zip'
    archive=tmp_path/relative;archive.parent.mkdir(parents=True);archive.write_bytes(b'PK synth')
    meta=dict(id_archivo=3,raiz='clientes',ruta=relative,nombre=archive.name,sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    row=dict(id_lectura=4,id_contribuyente=7,telegram_id=42,periodo='09/2026',estado='terminada',
             nombre_legal='Contribuyente sintético',autorizado=True,resultado={'importaciones_sifere':{'paquete':3}})
    calls=[]
    def query(sql):
        calls.append(sql)
        return [] if revocar and len(calls)>1 else [meta]
    flow=SimpleNamespace(clients_root=tmp_path,_query=query)
    return ComarbDeduccionesEntregas(flow),row,calls


def test_envia_paquete_real_validado_con_nombre_y_reautorizacion(tmp_path):
    async def run():
        delivery,row,calls=scenario(tmp_path)
        bot=SimpleNamespace(send_document=AsyncMock(),send_message=AsyncMock())
        await delivery.enviar_importaciones(bot,row)
        assert len(calls)==2
        assert 'fn_comarb_deducciones_importaciones_telegram(42,4,7)' in calls[0]
        assert bot.send_document.await_count==1
        assert bot.send_document.await_args.kwargs['filename']=='cliente-importar-sifere-901-2026-09.zip'
        assert 'No se modificó ninguna DDJJ' in bot.send_document.await_args.kwargs['caption']
        assert 'Descomprimí el ZIP e importá cada TXT por separado' in bot.send_document.await_args.kwargs['caption']
    asyncio.run(run())


def test_permiso_revocado_entre_lectura_y_envio_no_entrega(tmp_path):
    async def run():
        delivery,row,calls=scenario(tmp_path,revocar=True)
        bot=SimpleNamespace(send_document=AsyncMock(),send_message=AsyncMock())
        await delivery.enviar_importaciones(bot,row)
        bot.send_document.assert_not_awaited()
        assert len(calls)==2
        assert 'Documentos' in bot.send_message.await_args.kwargs['text']
    asyncio.run(run())


def test_sin_exportaciones_no_toca_la_entrega_anterior(tmp_path):
    async def run():
        delivery,row,calls=scenario(tmp_path);row['resultado']={}
        bot=SimpleNamespace(send_document=AsyncMock(),send_message=AsyncMock())
        await delivery.enviar_importaciones(bot,row)
        assert not calls
        bot.send_document.assert_not_awaited();bot.send_message.assert_not_awaited()
    asyncio.run(run())


def test_hash_alterado_no_envia_y_conserva_fallback_documentos(tmp_path):
    async def run():
        delivery,row,calls=scenario(tmp_path)
        next(tmp_path.rglob('*.zip')).write_bytes(b'alterado')
        bot=SimpleNamespace(send_document=AsyncMock(),send_message=AsyncMock())
        await delivery.enviar_importaciones(bot,row)
        bot.send_document.assert_not_awaited()
        assert 'Documentos' in bot.send_message.await_args.kwargs['text']
    asyncio.run(run())
