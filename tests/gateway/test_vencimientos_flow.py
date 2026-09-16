import unittest
from unittest.mock import patch

from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow


class VencimientosFlowTests(unittest.TestCase):
    def test_search_scopes_by_telegram_id_and_accepts_slug(self):
        flow = VencimientosFlow()
        with patch.object(flow, "_query", return_value=[]) as query:
            flow._search(123, "cliente-demo")
        sql = query.call_args.args[0]
        self.assertIn("telegram_id=123", sql)
        self.assertIn("slug=lower", sql)
        self.assertNotIn("cliente-demo", sql)

    def test_calendar_scopes_by_user_and_contributor(self):
        flow = VencimientosFlow()
        with patch.object(flow, "_query", return_value=[]) as query:
            flow._calendar(123, 9)
        sql = query.call_args.args[0]
        self.assertIn("telegram_id=123", sql)
        self.assertIn("id_contribuyente=9", sql)

    def test_connection_fails_closed_without_runtime_config(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "vencimientos_database_unavailable"):
                VencimientosFlow._connection_args()


if __name__ == "__main__":
    unittest.main()
