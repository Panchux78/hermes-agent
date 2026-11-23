from types import SimpleNamespace as V
from time import monotonic
from unittest.mock import AsyncMock,patch
from unittest import IsolatedAsyncioTestCase

from plugins.platforms.telegram.facturacion_flow import FacturacionFlow


class FacturacionTests(IsolatedAsyncioTestCase):
    def fake_doc(self,user=44,chat=44):
        archivo=V(file_name='modelo.xlsx',file_size=12,
                  get_file=AsyncMock(return_value=V(download_as_bytearray=AsyncMock(return_value=bytearray(b'X')))))
        return V(from_user=V(id=user),chat_id=chat,document=archivo,reply_document=AsyncMock(),reply_text=AsyncMock())

    async def test_no_descarga_sin_chat_privado_ni_pedido(self):
        flow=FacturacionFlow(None)
        for msg in (self.fake_doc(44,45),self.fake_doc()):
            assert not await flow.document(msg)
            msg.document.get_file.assert_not_called()

    async def test_envia_revision_no_emision(self):
        flow=FacturacionFlow(None);flow.pending[('44','44')]=monotonic()+60
        message=self.fake_doc()
        res={'comprobantes':[{'estado':'por_verificar'},{'estado':'bloqueada'}],
             'observaciones':[{'fila':4},{'fila':4}],'emitidos':0}
        with patch.object(flow,'_actor',return_value=44) as actor,patch(
                'plugins.platforms.telegram.facturacion_flow.cargar') as cargar:
            cargar.return_value.revisar.return_value=res
            cargar.return_value.control_excel.return_value=b'excel'
            assert await flow.document(message)
        assert actor.call_count==2
        args,kwargs=message.reply_document.await_args
        assert args[0].filename=='control-facturacion.xlsx'
        assert '1 fila para corregir' in kwargs['caption']
        assert 'No se emitió nada.' in kwargs['caption']
        assert not flow.pending
