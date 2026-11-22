import tempfile
from pathlib import Path
from datetime import date
from types import SimpleNamespace as Obj
from unittest import IsolatedAsyncioTestCase,TestCase
from unittest.mock import AsyncMock,patch

from plugins.platforms.telegram import execution_origin as execution, lunix_origin as labels, lunix_fiscal_history as fiscal
from plugins.platforms.telegram.period_input import parse_period


class LunixOrigin(TestCase):
    def test_ccma_and_sct_supply_different_internal_period_formats(self):
        self.assertEqual(fiscal.periodo_anual('01/2025'),'2025')
        selection=parse_period('2024','sct_estado_cumplimiento',today=date(2026,11,22))
        self.assertEqual(fiscal.periodo_anual(selection.sct()[1]),'2024')
        with self.assertRaises(ValueError):
            fiscal.periodo_anual('01/2')

    def test_reservation_uses_the_same_wire_across_distinct_private_message_ids(self):
        with tempfile.TemporaryDirectory() as target:
            directory=Path(target)
            labels.arm(12345,'callback',19,b'route:one',directory=directory)
            self.assertFalse(labels.claim(12346,'callback',19,b'route:one',directory=directory))
            self.assertFalse(labels.claim(12345,'callback',19,b'route:two',directory=directory))
            self.assertTrue(labels.claim(12345,'callback',20,b'route:one',directory=directory))
            self.assertFalse(labels.claim(12345,'callback',19,b'route:one',directory=directory))

    def test_insecure_directory_and_expiration_do_not_produce_marks(self):
        with tempfile.TemporaryDirectory() as target:
            directory=Path(target)
            directory.chmod(0o755)
            with self.assertRaises(ValueError):
                labels.arm(12345,'mensaje',None,b'read',directory=directory)
            directory.chmod(0o700)
            labels.arm(12345,'mensaje',None,b'read',directory=directory)
            with patch.object(labels.time,'monotonic_ns',return_value=10**20):
                self.assertFalse(labels.claim(12345,'mensaje',None,b'read',directory=directory))


class UpdateOrigin(IsolatedAsyncioTestCase):
    async def test_human_lunix_same_sender_remains_telegram_after_reserved_update(self):
        @execution.incoming
        async def callback(update, context):
            return execution.environment({})

        message=Obj(message_id=19,text='read',from_user=Obj(id=12345))
        update=Obj(update_id=4,effective_message=message,callback_query=None,effective_chat=Obj(id=12345))
        with tempfile.TemporaryDirectory() as target, patch.object(labels,'root',return_value=Path(target)):
            labels.arm(12345,'mensaje',None,b'read')
            automated=await callback(update,None)
            manual=await callback(update,None)
        self.assertEqual((automated['CONTABOT_ORIGEN_CANAL'],automated['CONTABOT_ORIGEN_ENTRADA']),
                         ('automatico','update:4/mensaje:19'))
        self.assertEqual(manual['CONTABOT_ORIGEN_CANAL'],'telegram')
        self.assertEqual(execution.environment({})['CONTABOT_ORIGEN_CANAL'],'prueba')

    async def test_manual_fiscal_query_never_enters_automation_history(self):
        target=AsyncMock()
        arguments={'chat_id':'12345'}
        with patch.object(fiscal,'BotRunHistory') as history:
            await fiscal.tracked(None,'sct',target,arguments)
        target.assert_awaited_once_with(**arguments)
        history.assert_not_called()

    async def test_automatic_cannot_call_fiscal_worker_without_its_history(self):
        target,send=AsyncMock(),AsyncMock()
        arguments={'chat_id':'12345','telegram_id':'12345','contributor_id':20,'period_from':'2025-01'}
        obj=Obj(start=AsyncMock(side_effect=RuntimeError('history')))
        with patch.object(execution,'_origin',execution.ContextVar('test')), patch.object(fiscal,'BotRunHistory',return_value=obj):
            execution._origin.set(('automatico','control_diario_lunix','update:8/mensaje:9','12345'))
            await fiscal.tracked(Obj(send=send),'ccma',target,arguments)
        target.assert_not_called()
        send.assert_awaited_once()

    async def test_automatic_history_closes_success_only_after_verified_delivery(self):
        async def target(**kwargs):
            kwargs['qa_record'].output=Path('/private/qa.xlsx')
            kwargs['qa_record'].relative='estudios/qa.xlsx'
            kwargs['qa_record'].delivered=True
        arguments={'chat_id':'12345','telegram_id':'12345','contributor_id':20,'period_from':'2025-01'}
        obj=Obj(start=AsyncMock(return_value=22),finish_single=AsyncMock())
        with patch.object(execution,'_origin',execution.ContextVar('test')), patch.object(fiscal,'BotRunHistory',return_value=obj):
            execution._origin.set(('automatico','control_diario_lunix','update:8/mensaje:9','12345'))
            await fiscal.tracked(Obj(send=AsyncMock()),'ccma',target,arguments)
        entry=obj.finish_single.await_args
        self.assertEqual((entry.args,entry.kwargs['state'],entry.kwargs['period']),((22,),'completado','2025'))
        self.assertEqual(entry.kwargs['output_relative'],'estudios/qa.xlsx')
