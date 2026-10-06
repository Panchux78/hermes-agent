import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.platforms.telegram.menu_buttons import MENU_ALIGNMENT_PADDING, aligned_menu_label
from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow
from tests.gateway.documentos_falsos import instalar as instalar_documentos
import pytest


class VencimientosFlowTests(unittest.TestCase):
    def test_source_calendar_parses_the_canonical_csv_contract(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "source.py"
            mapping = root / "map.json"
            mapping.write_text("{}", encoding="utf-8")
            script.write_text(
                "import csv,json,os,sys\n"
                "out=sys.argv[3]\n"
                "headers=['ID Impuesto','Impuesto','ID Concepto','Concepto','Período','Anticipo/Cuota','Tipo Operación','Vencimiento','Formularios']\n"
                "with open(out,'w',encoding='utf-8-sig',newline='') as f:\n"
                " w=csv.writer(f,delimiter=';');w.writerow(headers);w.writerow(['20','IVA','30','Saldo','202608','8','PAGO','21/09/2026','F.2051'])\n"
                "os.chmod(out,0o600)\n"
                "print(json.dumps({'ok':True,'rows':1,'output':out}))\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            mapping.chmod(0o600)
            flow = VencimientosFlow(
                runtime_python=Path(sys.executable),
                source_script=script,
                source_map=mapping,
            )
            rows = flow._source_calendar("20123456786")
        self.assertEqual(rows, [{
            "id_impuesto": "20", "impuesto": "IVA", "id_concepto": "30",
            "concepto": "Saldo", "periodo": "202608", "anticipo_cuota": "8",
            "tipo": "PAGO", "fecha": "2026-09-21", "formularios": "F.2051",
            "estado": "pendiente",
        }])

    def test_format_buttons_have_explicit_alignment_and_stable_callbacks(self):
        import plugins.platforms.telegram.vencimientos_flow as module

        self.assertEqual(MENU_ALIGNMENT_PADDING[("vencimientos_formato", "Excel")], (14, 0))
        self.assertEqual(MENU_ALIGNMENT_PADDING[("vencimientos_formato", "ICS para Google Calendar")], (0, 0))
        with (patch.object(module, "InlineKeyboardButton", side_effect=lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data)),
              patch.object(module, "InlineKeyboardMarkup", side_effect=lambda rows: SimpleNamespace(inline_keyboard=rows))):
            rows = VencimientosFlow._formats("abc").inline_keyboard
        self.assertEqual(
            [row[0].text for row in rows[:2]],
            [
                aligned_menu_label("vencimientos_formato", "📊", "Excel"),
                aligned_menu_label("vencimientos_formato", "📅", "ICS para Google Calendar"),
            ],
        )
        self.assertEqual(
            [row[0].callback_data for row in rows[:2]],
            ["ve:format:abc:xlsx", "ve:format:abc:ics"],
        )

    def test_search_scopes_by_telegram_id_and_accepts_name_slug_or_cuit(self):
        flow = VencimientosFlow()
        with patch.object(flow, "_query", return_value=[]) as query:
            flow._search(123, "cliente-demo")
        sql = query.call_args.args[0]
        self.assertIn("fn_buscar_contribuyente_telegram", sql)
        self.assertIn("123,convert_from(decode(", sql)
        self.assertIn("'cuit',cuit", sql)
        self.assertNotIn("cliente-demo", sql)

    def test_search_is_independent_from_due_date_rows(self):
        flow = VencimientosFlow()
        with patch.object(flow, "_query", return_value=[]) as query:
            flow._search(123, "berenstein")
        sql = query.call_args.args[0]
        self.assertNotIn("vw_vencimientos_telegram", sql)
        self.assertNotIn("tbl_vencimientos", sql)

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
    def documentos(self, temporary, **opciones):
        """CLI falso de documentos_cliente.py (Ágora #115), como subprocess real."""
        monkeypatch = pytest.MonkeyPatch()
        self.addCleanup(monkeypatch.undo)
        return instalar_documentos(monkeypatch, Path(temporary.name), **opciones)

    async def test_empty_published_calendar_fetches_arca_before_exporting(self):
        from tempfile import TemporaryDirectory

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.documentos(temporary)
        flow = VencimientosFlow(runtime_python=Path("/runtime/python"), clients_root=Path(temporary.name))
        state = SimpleNamespace(
            user_id="123", nonce="abc", stage="format", created_at=__import__("time").monotonic(),
            contributor_id=158, contributor_name="Berenstein Jorge", contributor_slug="berenstein-jorge",
            contributor_cuit="20123456786",
        )
        live = [{"fecha": "2026-09-21", "estado": "pendiente"}]
        flow.states["10::123"] = state
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))

        def generate(kind, output, contributor_id, slug, rows):
            self.assertEqual(rows, live)
            output.write_bytes(b"xlsx")

        with (patch.object(flow, "_calendar", return_value=[]),
              patch.object(flow, "_source_calendar", return_value=live) as source,
              patch.object(flow, "_generate", side_effect=generate)):
            await flow.callback(adapter, query, "ve:format:abc:xlsx", 10, None, "123")
        source.assert_called_once_with("20123456786")
        self.assertEqual(query.edit_message_text.await_args_list[-1].args[0], "Excel enviado.")

    async def test_unique_subject_asks_for_excel_or_ics_without_listing_rows(self):
        flow = VencimientosFlow()
        state = SimpleNamespace(user_id="123", nonce="abc", created_at=__import__("time").monotonic())
        flow.states["10::123"] = state
        message = SimpleNamespace(
            chat_id=10, message_thread_id=None,
            from_user=SimpleNamespace(id=123), text="cliente-demo",
            reply_text=AsyncMock(),
        )
        row = {"id": 9, "nombre": "Cliente Demo", "slug": "cliente-demo", "cuit": "20123456786"}
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
        from tempfile import TemporaryDirectory

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clients_root = Path(temporary.name) / "clientes"
        documentos = self.documentos(temporary)
        flow = VencimientosFlow(runtime_python=Path("/runtime/python"), clients_root=clients_root)
        state = SimpleNamespace(
            user_id="123", nonce="abc", stage="format", created_at=__import__("time").monotonic(),
            contributor_id=9, contributor_name="Cliente Demo", contributor_slug="cliente-demo",
            contributor_cuit="20123456786",
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
        self.assertEqual(adapter.send_document.await_args.kwargs["file_name"], "cliente-demo-vencimientos-arca-2026.xlsx")
        # Ágora #115: lo guarda el módulo de documentos; nada en el árbol viejo.
        self.assertFalse(clients_root.exists())
        self.assertEqual(documentos.destinos(), [{
            "seccion": "arca", "anio": 2026, "mes": None, "base": "cliente-demo-vencimientos-arca-2026",
            "ext": "xlsx", "etiqueta": "Vencimientos · 2026", "tipo": "Vencimientos", "origen": "ARCA",
            "productor": "vencimientos", "id_contribuyente": 9,
            "periodo_desde": "2026-01-01", "periodo_hasta": "2026-12-01",
        }])
        saved = documentos.guardados()
        self.assertEqual([p.relative_to(documentos.raiz).as_posix() for p in saved],
                         ["estudios/1/9/2026/anual/arca/cliente-demo-vencimientos-arca-2026.xlsx"])
        self.assertEqual(saved[0].read_bytes(), b"xlsx")
        self.assertEqual(query.edit_message_text.await_args_list[-1].args[0], "Excel enviado.")

    async def test_ics_spanning_years_is_saved_as_range_in_last_month_and_versioned(self):
        from tempfile import TemporaryDirectory

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        clients_root = Path(temporary.name) / "clientes"
        documentos = self.documentos(temporary)
        flow = VencimientosFlow(runtime_python=Path("/runtime/python"), clients_root=clients_root)
        state = SimpleNamespace(
            user_id="123", nonce="abc", stage="format", created_at=__import__("time").monotonic(),
            contributor_id=9, contributor_name="Cliente Demo", contributor_slug="cliente-demo",
            contributor_cuit="20123456786",
        )
        rows = [{"fecha": "2026-12-31"}, {"fecha": "2027-01-02"}]

        def generate(kind, output, contributor_id, slug, given_rows):
            output.write_bytes(b"ics")

        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
        base = "cliente-demo-vencimientos-arca-2026-12-a-2027-01"
        for expected in (f"{base}.ics", f"{base}-v02.ics"):
            flow.states["10::123"] = state
            with patch.object(flow, "_calendar", return_value=rows), patch.object(flow, "_generate", side_effect=generate):
                await flow.callback(adapter, SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock()),
                                    "ve:format:abc:ics", 10, None, "123")
            self.assertEqual(adapter.send_document.await_args.kwargs["file_name"], expected)
        destino = documentos.destinos()[0]
        self.assertEqual((destino["anio"], destino["mes"], destino["ext"]), (2027, 1, "ics"))
        self.assertEqual((destino["periodo_desde"], destino["periodo_hasta"]), ("2026-12-01", "2027-01-01"))
        self.assertEqual([p.relative_to(documentos.raiz).as_posix() for p in documentos.guardados()], [
            f"estudios/1/9/2027/01/arca/{base}-v02.ics", f"estudios/1/9/2027/01/arca/{base}.ics",
        ])
        self.assertFalse(clients_root.exists())

    async def test_quota_shows_contabot_message_and_never_delivers(self):
        from tempfile import TemporaryDirectory

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.documentos(temporary, cuota="guardar")
        flow = VencimientosFlow(runtime_python=Path("/runtime/python"))
        state = SimpleNamespace(
            user_id="123", nonce="abc", stage="format", created_at=__import__("time").monotonic(),
            contributor_id=9, contributor_name="Cliente Demo", contributor_slug="cliente-demo",
            contributor_cuit="20123456786",
        )
        flow.states["10::123"] = state
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))

        def generate(kind, output, contributor_id, slug, given_rows):
            output.write_bytes(b"xlsx")

        with patch.object(flow, "_calendar", return_value=[{"fecha": "2026-09-18"}]), \
                patch.object(flow, "_generate", side_effect=generate):
            await flow.callback(adapter, query, "ve:format:abc:xlsx", 10, None, "123")
        adapter.send_document.assert_not_awaited()
        self.assertEqual(
            query.edit_message_text.await_args_list[-1].args[0],
            "El estudio no tiene espacio suficiente. Liberá archivos desde Documentos o pedí ampliar la cuota.",
        )

    async def test_document_module_failure_never_reports_success(self):
        from tempfile import TemporaryDirectory

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.documentos(temporary, falla="guardar")
        flow = VencimientosFlow(runtime_python=Path("/runtime/python"))
        flow.states["10::123"] = SimpleNamespace(
            user_id="123", nonce="abc", stage="format", created_at=__import__("time").monotonic(),
            contributor_id=9, contributor_name="Cliente Demo", contributor_slug="cliente-demo",
            contributor_cuit="20123456786",
        )
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        adapter = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
        with patch.object(flow, "_calendar", return_value=[{"fecha": "2026-09-18"}]), \
                patch.object(flow, "_generate", side_effect=lambda k, o, *a: o.write_bytes(b"x")):
            await flow.callback(adapter, query, "ve:format:abc:xlsx", 10, None, "123")
        adapter.send_document.assert_not_awaited()
        self.assertNotIn("enviado", query.edit_message_text.await_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()
