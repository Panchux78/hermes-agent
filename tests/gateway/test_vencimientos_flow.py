import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow


class VencimientosFlowTests(unittest.TestCase):
    def test_search_scopes_by_telegram_id_and_accepts_name_slug_or_cuit(self):
        flow = VencimientosFlow()
        with patch.object(flow, "_query", return_value=[]) as query:
            flow._search(123, "cliente-demo")
        sql = query.call_args.args[0]
        self.assertIn("telegram_id=123", sql)
        self.assertIn("slug=lower", sql)
        self.assertIn("regexp_replace(cuit", sql)
        self.assertNotIn("cliente-demo", sql)

    def test_calendar_scopes_by_user_and_contributor(self):
        flow = VencimientosFlow()
        with patch.object(flow, "_query", return_value=[]) as query:
            flow._calendar(123, 9)
        sql = query.call_args.args[0]
        self.assertIn("telegram_id=123", sql)
        self.assertIn("id_contribuyente=9", sql)
        self.assertIn("formularios", sql)
        self.assertNotIn("LIMIT 40", sql)

    def test_connection_fails_closed_without_runtime_config(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "vencimientos_database_unavailable"):
                VencimientosFlow._connection_args()

    def test_telegram_is_query_only(self):
        self.assertFalse(hasattr(VencimientosFlow, "notification_loop"))
        self.assertFalse(hasattr(VencimientosFlow, "_claim"))


class VencimientosConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unique_subject_asks_for_excel_or_ics_without_listing_rows(self):
        flow = VencimientosFlow()
        state = SimpleNamespace(user_id="123", nonce="abc", created_at=__import__("time").monotonic())
        flow.states["10::123"] = state
        message = SimpleNamespace(
            chat_id=10, message_thread_id=None,
            from_user=SimpleNamespace(id=123), text="cliente-demo",
            reply_text=AsyncMock(),
        )
        row = {"id": 9, "nombre": "Cliente Demo", "slug": "cliente-demo"}
        formats = object()
        with (patch.object(flow, "_search", return_value=[row]),
              patch.object(flow, "_formats", return_value=formats)):
            await flow.text(SimpleNamespace(), message)
        current = flow.states["10::123"]
        self.assertEqual(current.stage, "format")
        text = message.reply_text.await_args.args[0]
        self.assertNotIn("•", text)
        self.assertIs(message.reply_text.await_args.kwargs["reply_markup"], formats)

    async def test_format_callback_delivers_selected_file(self):
        flow = VencimientosFlow(runtime_python=Path("/runtime/python"))
        state = SimpleNamespace(
            user_id="123", nonce="abc", stage="format", created_at=__import__("time").monotonic(),
            contributor_id=9, contributor_name="Cliente Demo", contributor_slug="cliente-demo",
        )
        flow.states["10::123"] = state
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
        rows = [{"fecha": "2026-09-18"}]

        def generate(kind, output, contributor_id, slug, given_rows):
            self.assertEqual((kind, contributor_id, slug, given_rows), ("xlsx", 9, "cliente-demo", rows))
            output.write_bytes(b"xlsx")

        with patch.object(flow, "_calendar", return_value=rows), patch.object(flow, "_generate", side_effect=generate):
            handled = await flow.callback(adapter, query, "ve:format:abc:xlsx", 10, None, "123")
        self.assertTrue(handled)
        self.assertNotIn("10::123", flow.states)
        self.assertEqual(adapter.send_document.await_args.kwargs["file_name"], "cliente-demo-vencimientos-arca.xlsx")
        self.assertEqual(query.edit_message_text.await_args_list[-1].args[0], "Excel enviado.")


if __name__ == "__main__":
    unittest.main()
