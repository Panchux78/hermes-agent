import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.platforms.telegram.apocrifos_flow import ApocrifosFlow, resultado


@pytest.mark.asyncio
async def test_canal_privado_y_periodo_estandar():
    flow = ApocrifosFlow()
    bot = SimpleNamespace(send_message=AsyncMock())
    adapter = SimpleNamespace(_bot=bot)
    click = SimpleNamespace(answer=AsyncMock())
    assert await flow.callback(adapter, click, 'apo:uno', -100, None, '42')
    bot.send_message.assert_not_awaited()
    scope = MagicMock()
    scope.search.return_value = [{'id': 7, 'nombre': 'Cliente de prueba', 'cuit': '00000000000', 'slug': 'prueba'}]
    flow._scope = lambda: scope
    assert await flow.callback(adapter, click, 'apo:uno', 42, None, '42')
    assert 'nombre, CUIT o slug' in bot.send_message.call_args.kwargs['text']
    msg = SimpleNamespace(chat_id=42, from_user=SimpleNamespace(id=42), text='cliente', message_id=88, reply_text=AsyncMock())
    assert await flow.text(adapter, msg, update_id=12)
    assert 'MM/AAAA' in bot.send_message.call_args.kwargs['text']
    flow._write = MagicMock(return_value=[{'id_revision': 220}])
    msg.text = '31/2026'
    assert await flow.text(adapter, msg, update_id=13)
    flow._write.assert_not_called()
    msg.text = '09/2026'
    assert await flow.text(adapter, msg, update_id=14)
    flow._write.assert_not_called()
    assert '¿Consulto?' in msg.reply_text.call_args.args[0]
    candidate = next(iter(flow.states.values())).period_candidate
    confirm = f'apo:period:yes:{candidate.nonce}'
    assert await flow.callback(adapter, click, confirm, 42, None, '42')
    assert "42,'pedir',NULL,7,'09/2026','update:14/mensaje:88'" in flow._write.call_args.args[0]
    assert 'Revisión pedida' in msg.reply_text.call_args.args[0]
    assert not flow.states


@pytest.mark.asyncio
@pytest.mark.parametrize('vigente', [True, False])
async def test_entrega_mismo_archivo_y_reautoriza(vigente):
    with TemporaryDirectory() as name:
        root = Path(name)
        relative = 'estudios/prueba/revision.xlsx'
        file = root / relative
        file.parent.mkdir(parents=True)
        file.write_bytes(b'el archivo ya registrado en Documentos')
        row = {'nombre':'Cliente de prueba','periodo':'09/2026','estado':'parcial','filas':3,'proveedores':2,
               'sin_cuit':1,'coincidencias':1,'archivo':{'ruta':relative,'nombre':'revision.xlsx',
                                                        'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}}
        flow = ApocrifosFlow(clients_root=root)
        flow._query = lambda sql: [{'telegram_id': 42, 'id_revision': 220}] if 'entregas()' in sql else [row] if vigente else []
        flow._write = MagicMock(side_effect=lambda s: [row] if "'reclamar'" in s else [True])
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True,message_id='102',delivered_filename='revision.xlsx')),
                                  _bot=SimpleNamespace(send_message=AsyncMock()))
        await flow.poll(adapter)
        if vigente:
            adapter.send_document.assert_awaited_once()
            sent = adapter.send_document.call_args.kwargs
            assert sent['file_path'] == str(file) and sent['file_name'] == row['archivo']['nombre']
            assert 'Revisión parcial' in sent['caption']
            assert "'enviada',220,NULL,NULL,NULL,102" in flow._write.call_args.args[0]
        else:
            adapter.send_document.assert_not_awaited()
            adapter._bot.send_message.assert_not_awaited()


def test_ausencia_de_libro_no_equivale_a_cero():
    text = resultado({'nombre':'Cliente de prueba','periodo':'09/2026','estado':'no_verificada','motivo':'No hay Libro IVA Compras.'})
    assert 'No se pudo verificar.' in text and 'Historial' in text and '0 proveedores' not in text
