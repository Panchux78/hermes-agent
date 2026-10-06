import asyncio
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.portal_iva_flow import FlowState, PortalIvaFlow
from plugins.platforms.telegram.fiscal_credentials import sanitized_psql_failure


VALID_CUIT = "20123456786"


@pytest.fixture(autouse=True)
def verify_representation_mock(monkeypatch):
    verification = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "plugins.platforms.telegram.portal_iva_flow.verify_representation",
        verification,
    )
    return verification


def _scope_item(**overrides):
    item = {
        "id": 1, "nombre": "Uno", "cuit": VALID_CUIT, "slug": "uno",
        "study_id": 1, "relation_id": 11, "relation_revision": 1,
        "verified": False, "representative_id": 1, "holder_cuit": VALID_CUIT,
        "access_id": 10,
    }
    item.update(overrides)
    return item


def _scope_query(sql):
    if "fn_actor_telegram_vinculado" in sql:
        return [{"linked": True}]
    if "fn_actor_telegram_permiso" in sql:
        return [{"allowed": True}]
    if "fn_verificar_representacion_fiscal" in sql:
        return [{"verified": True}]
    return []


def test_actor_without_execute_permission_is_rejected_before_state_creation():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        def query(sql):
            if "fn_actor_telegram_vinculado" in sql:
                return [{"linked": True}]
            if "fn_actor_telegram_permiso" in sql:
                return [{"allowed": False}]
            return []
        flow._query = query
        callback = _query("pi:generar")
        await flow.start(adapter, callback, "10", None, "7", "generar")
        assert not flow.states
        callback.answer.assert_awaited_once_with("No tenés permiso para ejecutar operaciones fiscales.")
    asyncio.run(scenario())


def test_database_failure_at_start_answers_and_records_rejection():
    async def scenario():
        history = SimpleNamespace(reject=AsyncMock())
        flow = PortalIvaFlow(history=history)
        adapter = FakeAdapter()
        flow._query = lambda _sql: (_ for _ in ()).throw(
            RuntimeError("PORTAL_IVA_DATABASE_PERMISSIONS")
        )
        callback = _query("pi:lote")

        await flow.start(adapter, callback, "10", None, "7", "descargar-lote")

        assert not flow.states
        callback.answer.assert_awaited_once_with("Portal IVA no pudo iniciarse.")
        assert "configuración fiscal local" in adapter._bot.send_message.await_args.kwargs["text"]
        assert history.reject.await_args.kwargs["reason_code"] == "base_fiscal_no_disponible"

    asyncio.run(scenario())


def test_psql_failure_diagnostic_exposes_only_sqlstate():
    assert sanitized_psql_failure("ERROR:  42501: permission denied for function secret\n") == (
        "42501", "ERROR 42501"
    )
    assert sanitized_psql_failure("password=hunter2\nprivate SQL") == (
        "unknown", "psql_error"
    )


def test_batch_start_explains_why_a_client_may_be_missing():
    async def scenario():
        flow = PortalIvaFlow(batch_executor=Path(__file__))
        flow._query = _scope_query
        adapter = FakeAdapter()
        await flow.start(adapter, _query("pi:lote"), "10", None, "7", "descargar-lote")
        message = adapter._bot.send_message.await_args.kwargs["text"]
        assert "Sólo podés elegir responsables inscriptos" in message

    asyncio.run(scenario())


def test_telegram_offers_only_inscripto_and_explains_excluded_conditions():
    async def scenario():
        for operation in ("descargar-lote", "descargar-presentados"):
            for reason, fragment in (("monotributo", "monotributista"),
                                     ("sin_consultar", "no se consultó")):
                flow = PortalIvaFlow()
                adapter = FakeAdapter()
                state = FlowState(user_id="7", nonce="a" * 10, stage="client", operation=operation)
                key = flow._key("10", None, "7")
                flow.states[key] = state
                flow._search = lambda _term, _actor: [_scope_item(access_id=None)]
                flow._iva_reason = lambda _item, _actor: reason
                assert await flow.text(adapter, _message("Uno")) is True
                assert fragment in adapter._bot.send_message.await_args.kwargs["text"]
                assert not flow.tasks and state.stage == "client"
            flow = PortalIvaFlow()
            adapter = FakeAdapter()
            state = FlowState(user_id="7", nonce="a" * 10, stage="client", operation=operation)
            flow.states[flow._key("10", None, "7")] = state
            flow._search = lambda _term, _actor: [_scope_item()]
            assert await flow.text(adapter, _message("Uno")) is True
            assert state.stage == ("batch_clients" if operation == "descargar-lote" else "period")

    asyncio.run(scenario())


def test_batch_start_fails_fast_when_executor_is_missing(tmp_path):
    async def scenario():
        history = SimpleNamespace(reject=AsyncMock())
        flow = PortalIvaFlow(batch_executor=tmp_path / "missing.py", history=history)
        flow._query = _scope_query
        adapter = FakeAdapter()
        callback = _query("pi:lote")

        await flow.start(adapter, callback, "10", None, "7", "descargar-lote")

        assert not flow.states
        callback.answer.assert_awaited_once_with(
            "El lote de Portal IVA requiere mantenimiento local."
        )
        assert "falta su ejecutor local" in adapter._bot.send_message.await_args.kwargs["text"]
        assert history.reject.await_args.kwargs["reason_code"] == "ejecutor_lote_no_disponible"

    asyncio.run(scenario())


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


def test_safe_error_code_exposes_only_stable_portal_iva_codes():
    assert PortalIvaFlow._safe_error_code(
        RuntimeError("PORTAL_IVA_OUTPUT_INCOMPLETE")
    ) == "PORTAL_IVA_OUTPUT_INCOMPLETE"
    assert PortalIvaFlow._safe_error_code(
        RuntimeError("/home/pancho/private/path")
    ) == "RuntimeError"


def test_captcha_terminal_errors_are_explained_to_the_user():
    assert PortalIvaFlow._error_message(
        {"motivo": "CAPTCHA_REINTENTOS_AGOTADOS"}, "fallback", "generar"
    ) == "Generar CSV de período nuevo: ARCA rechazó tres respuestas de captcha."
    assert PortalIvaFlow._error_message(
        {"motivo": "PORTAL_IVA_CAPTCHA_TIMEOUT"}, "fallback", "descargar-presentados"
    ) == "Descargar CSV presentados: venció el tiempo para responder el captcha."


def test_missing_presented_period_is_explained_and_suggests_the_correct_action():
    assert PortalIvaFlow._error_message(
        {"motivo": "PERIODO_NO_PRESENTADO_2026-08"},
        "fallback",
        "descargar-presentados",
    ) == (
        "Descargar CSV presentados: el período 08/2026 no figura como presentado en ARCA. "
        "Si todavía no fue presentado, usá “Generar CSV de período nuevo”."
    )


class FakeProcess:
    def __init__(self, payload, returncode=0):
        self.payload = payload
        self.returncode = returncode
        self.pid = 4242
        self.stdin = FakeStdin()

    async def communicate(self):
        return self.payload, b""

    async def wait(self):
        return self.returncode


class FakeAdapter:
    def __init__(self):
        self._bot = SimpleNamespace(
            send_message=AsyncMock(return_value=FakeMessage()),
            send_photo=AsyncMock(),
        )

        async def send_document(**kwargs):
            return SimpleNamespace(success=True, delivered_filename=kwargs["file_name"])

        self.send_document = AsyncMock(side_effect=send_document)


class FakeStdin:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None


def _message(text, user_id="7", chat_id="10"):
    return SimpleNamespace(text=text, chat_id=chat_id, from_user=SimpleNamespace(id=user_id), message_thread_id=None)


def _query(data, user_id="7", chat_id="10"):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        message=SimpleNamespace(chat_id=chat_id, message_thread_id=None),
    )


def _saved_fields(root: Path, prefix: str, relative: str, content: bytes) -> dict:
    """Campos de un documento guardado, como los emite portal_iva.py (Ágora #115)."""
    import hashlib
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {f"{prefix}_name": path.name, f"{prefix}_path": str(path),
            f"{prefix}_ruta_clientes": relative, f"{prefix}_id_archivo": 1,
            f"{prefix}_bytes": len(content), f"{prefix}_sha256": hashlib.sha256(content).hexdigest()}


def _result(root: Path, state: FlowState, *, complete=True):
    year, month = state.period.split("-")
    folder = f"estudios/1/{state.cuit}/{year}/{month}/arca"
    files = []
    for label in ("ventas", "compras"):
        record = {"libro": label, "filas": 0, "conciliacion": "no_verificable"}
        record.update(_saved_fields(root, "zip", f"{folder}/{state.slug}-portal-iva-{label}-{state.period}.zip", b"PK"))
        record.update(_saved_fields(root, "csv", f"{folder}/{state.slug}-portal-iva-{label}-{state.period}.csv", b"cabecera\n"))
        record.update({key.replace("csv_", "entregable_", 1): value
                       for key, value in record.items() if key.startswith("csv_")})
        files.append(record)
    return {
        "ok": True, "etapa": "completado" if complete else "resolver",
        "archivos": files, "advertencias": ["CSV_SIN_FILAS_ventas"],
        "representado_verificado": state.cuit,
    }


def test_command_is_exact_and_shell_free(tmp_path):
    flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
    flow.executor.touch()
    flow.uv.touch()
    assert flow._command("cliente", "2026-08", "generar") == [
        str(tmp_path / "uv"), "run", "--with", "selenium", "xvfb-run", "-a",
        "python3", str(tmp_path / "portal_iva.py"), "--cliente", "cliente", "--periodo", "2026-08", "--operacion", "generar", "--captcha-stdin",
    ]


def test_period_accepts_only_month_year_and_stores_executor_period(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="period", contributor_id=1, slug="cliente", cuit=VALID_CUIT)
        flow._by_id = lambda _ident, _uid: [_scope_item()]
        key = flow._key("10", None, "7")
        flow.states[key] = state

        def fake_task(coro):
            coro.close()
            return SimpleNamespace(add_done_callback=lambda _callback: None)

        monkeypatch.setattr(asyncio, "create_task", fake_task)
        assert await flow.text(adapter, _message("08/2026")) is True
        assert state.period == "2026-08"
        assert state.stage == "running"

    asyncio.run(scenario())


def test_period_rejects_every_non_month_year_format():
    async def scenario():
        for value in ("2026-08", "13/2026", "8/2026", "08/26", "2026"):
            flow = PortalIvaFlow()
            adapter = FakeAdapter()
            state = FlowState(user_id="7", nonce="a" * 10, stage="period", slug="cliente", cuit=VALID_CUIT)
            flow.states[flow._key("10", None, "7")] = state
            assert await flow.text(adapter, _message(value)) is True
            assert state.stage == "period"
            assert state.period is None
            assert adapter._bot.send_message.await_args.kwargs["text"] == "Ingresá el período como MM/AAAA. Ejemplo: 08/2026."

    asyncio.run(scenario())


def test_period_prompt_is_month_year_only():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="client")
        await flow._select(adapter, "10", None, state, _scope_item())
        assert adapter._bot.send_message.await_args.kwargs["text"] == "Ingresá el período como MM/AAAA. Ejemplo: 08/2026."

    asyncio.run(scenario())


def test_flow_never_exposes_executor_period_format_to_telegram():
    source = Path("plugins/platforms/telegram/portal_iva_flow.py").read_text(encoding="utf-8")
    assert "AAAA-MM" not in source


def test_search_handles_unique_multiple_and_missing(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        flow._query = _scope_query
        flow._search = lambda term, _uid: []
        await flow.start(adapter, _query("pi:generar"), "10", None, "7", "generar")
        assert await flow.text(adapter, _message("nadie"))
        flow._search = lambda term, _uid: [_scope_item()]
        assert await flow.text(adapter, _message("uno"))
        assert flow.states[flow._key("10", None, "7")].stage == "period"
        flow.states[flow._key("10", None, "7")] = FlowState(user_id="7", nonce="b" * 10, stage="client")
        flow._search = lambda term, _uid: [
            _scope_item(),
            _scope_item(id=2, nombre="Dos", cuit="20987654326", slug="dos",
                        relation_id=12, representative_id=2, holder_cuit="20987654326"),
        ]
        assert await flow.text(adapter, _message("x"))
        assert "Elegí" in adapter._bot.send_message.await_args.kwargs["text"]
    asyncio.run(scenario())


def test_callback_selection_expires_and_double_start_is_blocked():
    async def scenario():
        flow = PortalIvaFlow()
        flow._query = _scope_query
        adapter = FakeAdapter()
        expired = _query("pi:select:" + "a" * 10 + ":1")
        assert await flow.callback(adapter, expired, expired.data, "10", None, "7")
        expired.answer.assert_awaited_once()
        flow.tasks[flow._key("10", None, "7")] = asyncio.current_task()
        start = _query("pi:generar")
        assert await flow.callback(adapter, start, "pi:generar", "10", None, "7")
        start.answer.assert_awaited_once_with("Ya hay una descarga Portal IVA en curso.")
    asyncio.run(scenario())


def test_each_portal_operation_records_active_rejection_before_executor():
    async def scenario():
        expected = {
            "generar": "portal_iva_generar_csv",
            "descargar-presentados": "portal_iva_descargar_presentados",
        }
        for operation, operation_code in expected.items():
            history = SimpleNamespace(reject=AsyncMock())
            flow = PortalIvaFlow(history=history)
            flow._run = AsyncMock()
            adapter = FakeAdapter()
            key = flow._key("10", None, "7")
            flow.tasks[key] = asyncio.current_task()
            query = _query("pi:generar" if operation == "generar" else "pi:descargar")

            await flow.start(adapter, query, "10", None, "7", operation)

            flow._run.assert_not_awaited()
            assert history.reject.await_args.kwargs["operation"] == operation_code
            assert history.reject.await_args.kwargs["reason_code"] == "consulta_en_curso"

    asyncio.run(scenario())


def test_unlinked_actor_and_forged_current_selection_are_rejected():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        flow._query = lambda _sql: [{"linked": False}]
        await flow.start(adapter, _query("pi:generar"), "10", None, "7", "generar")
        assert not flow.states
        assert "no está vinculada" in adapter._bot.send_message.await_args.kwargs["text"]

        key = flow._key("10", None, "7")
        state = FlowState(user_id="7", nonce="a" * 10, stage="client", candidates=(1,))
        flow.states[key] = state
        forged = _query("pi:select:" + "a" * 10 + ":2")
        assert await flow.callback(adapter, forged, forged.data, "10", None, "7")
        forged.answer.assert_awaited_once_with("La opción ya no está disponible.")
        assert state.stage == "client"

    asyncio.run(scenario())


def test_delivery_rejects_traversal_symlink_and_paths_outside_estudios(tmp_path):
    flow = PortalIvaFlow(clients_root=tmp_path)
    state = FlowState(user_id="7", nonce="a" * 10, stage="running", slug="cliente", cuit="20123456789", period="2026-08")
    result = _result(tmp_path, state)
    result["archivos"][0]["entregable_name"] = "../ventas.csv"
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)
    result = _result(tmp_path, state)
    result["archivos"][0]["entregable_ruta_clientes"] = "estudios/../../ventas.csv"
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)
    result = _result(tmp_path, state)
    ventas = Path(result["archivos"][0]["entregable_path"])
    ventas.unlink()
    ventas.symlink_to(tmp_path / "elsewhere.csv")
    (tmp_path / "elsewhere.csv").write_text("x")
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)
    # La estructura vieja <slug>/<cuit>/arca/AAAA/MM/consultas ya no se acepta.
    result = _result(tmp_path, state)
    old = tmp_path / "cliente/20123456789/arca/2026/08/consultas/cliente-portal-iva-ventas-2026-08.csv"
    old.parent.mkdir(parents=True)
    old.write_text("cabecera\n")
    result["archivos"][0].update(entregable_path=str(old), entregable_ruta_clientes=None)
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)
    fresh = tmp_path / "otra-raiz"
    fresh.mkdir()
    result = _result(fresh, state)
    result["archivos"][1]["entregable_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="DELIVERY_HASH"):
        PortalIvaFlow(clients_root=fresh)._deliverables(state.slug, state.cuit, state.period, result)


def test_delivery_uses_saved_deliverable_and_relative_route(tmp_path):
    flow = PortalIvaFlow(clients_root=tmp_path)
    state = FlowState(user_id="7", nonce="a" * 10, stage="running", slug="cliente",
                      cuit="20123456789", period="2026-08")
    result = _result(tmp_path, state)
    deliverables = flow._deliverables(state.slug, state.cuit, state.period, result)
    assert [path.name for path, _, _ in deliverables] == [
        "cliente-portal-iva-ventas-2026-08.csv",
        "cliente-portal-iva-compras-2026-08.csv",
    ]
    assert [str(path.relative_to(tmp_path)) for path, _, _ in deliverables] == [
        "estudios/1/20123456789/2026/08/arca/cliente-portal-iva-ventas-2026-08.csv",
        "estudios/1/20123456789/2026/08/arca/cliente-portal-iva-compras-2026-08.csv",
    ]


def test_stdout_requires_one_json_object():
    assert PortalIvaFlow._parse_result(b'{"ok":true}\n') == {"ok": True}
    with pytest.raises(RuntimeError, match="STDOUT_INVALID"):
        PortalIvaFlow._parse_result(b'{"ok":true}\nnoise\n')
    with pytest.raises(RuntimeError, match="STDOUT_INVALID"):
        PortalIvaFlow._parse_result(b'not-json\n')


def test_progress_merged_by_xvfb_is_removed_from_stdout_and_reported():
    async def scenario():
        stdout = asyncio.StreamReader()
        stdout.feed_data(
            b"PORTAL_IVA_PROGRESS:buscar_presentado\n"
            b"PORTAL_IVA_PROGRESS:descargar_ventas\n"
            b'{"ok":true,"etapa":"completado"}\n'
        )
        stdout.feed_eof()
        stderr = asyncio.StreamReader()
        stderr.feed_data(b"uv diagnostic\n")
        stderr.feed_eof()
        proc = SimpleNamespace(stdout=stdout, stderr=stderr, wait=AsyncMock(return_value=0))
        state = FlowState(
            user_id="7",
            nonce="a" * 10,
            stage="running",
            progress_message=FakeMessage(),
        )

        raw, captured_stderr = await PortalIvaFlow()._communicate_with_progress(proc, state)

        assert raw == b'{"ok":true,"etapa":"completado"}\n'
        assert captured_stderr == b"uv diagnostic\n"
        assert [edit[0] for edit in state.progress_message.edits] == [
            "Buscando presentación…",
            "Descargando Libro IVA Ventas…",
        ]

    asyncio.run(scenario())


def test_large_unexpected_stdout_line_does_not_kill_batch_or_later_progress():
    async def scenario():
        stdout = asyncio.StreamReader(limit=64 * 1024)
        stdout.feed_data(b"x" * 250_000 + b"\nPORTAL_IVA_PROGRESS:descargar_ventas\n"
                         b"PORTAL_IVA_RESULT_READY\n")
        stdout.feed_eof()
        stderr = asyncio.StreamReader(limit=64 * 1024)
        stderr.feed_eof()
        proc = SimpleNamespace(stdout=stdout, stderr=stderr, wait=AsyncMock(return_value=0))
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          progress_message=FakeMessage())

        raw, captured_stderr = await PortalIvaFlow()._communicate_with_progress(proc, state)

        assert len(raw) > 250_000
        assert raw.endswith(b"PORTAL_IVA_RESULT_READY\n")
        assert captured_stderr == b""
        assert [edit[0] for edit in state.progress_message.edits] == [
            "Descargando Libro IVA Ventas…",
        ]

    asyncio.run(scenario())


def test_batch_events_update_case_counts_without_leaking_event_json():
    async def scenario():
        stdout = asyncio.StreamReader()
        stdout.feed_data(
            b'PORTAL_IVA_CASE_START:{"id_contribuyente":1,"periodo":"2026-01"}\n'
            b'PORTAL_IVA_FILE:{"id_contribuyente":1,"periodo":"2026-01","tipo":"ventas","estado":"descargado"}\n'
            b'PORTAL_IVA_PROGRESS:descargar_compras\n'
            b'PORTAL_IVA_FILE:{"id_contribuyente":1,"periodo":"2026-01","tipo":"compras","estado":"descargado"}\n'
            b'PORTAL_IVA_FILE:{"id_contribuyente":1,"periodo":"2026-02","tipo":"ventas","estado":"sin_libro"}\n'
            b'PORTAL_IVA_FILE:{"id_contribuyente":1,"periodo":"2026-02","tipo":"compras","estado":"sin_libro"}\n'
            b'PORTAL_IVA_FILE:{"id_contribuyente":2,"periodo":"2026-01","tipo":"ventas","estado":"error"}\n'
            b'PORTAL_IVA_FILE:{"id_contribuyente":2,"periodo":"2026-01","tipo":"compras","estado":"descargado"}\n'
            b'PORTAL_IVA_CASE:{"id_contribuyente":2,"periodo":"2026-01","ok":true}\n'
            b'{"ok":true}\n'
        )
        stdout.feed_eof()
        stderr = asyncio.StreamReader()
        stderr.feed_eof()
        proc = SimpleNamespace(stdout=stdout, stderr=stderr, wait=AsyncMock(return_value=0))
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", progress_message=FakeMessage(),
                          selected_clients=[_scope_item(nombre="Empresa Uno SA"),
                                            _scope_item(id=2, slug="dos", nombre="Empresa Dos SRL")],
                          period_from="2026-01", period_to="2026-02", batch_months=2)
        flow = PortalIvaFlow()
        events = []

        async def on_event(kind, payload):
            events.append((kind, payload))
            await flow._batch_progress_event(state, kind, payload)

        raw, captured = await flow._communicate_with_progress(proc, state, batch_event=on_event)
        assert raw == b'{"ok":true}\n'
        assert captured == b""
        assert [edit[0] for edit in state.progress_message.edits] == [
            "Se descargaron 0/4 archivos de Empresa Uno SA",
            "Se descargaron 1/4 archivos de Empresa Uno SA",
            "Se descargaron 2/4 archivos de Empresa Uno SA",
            "Se descargaron 2/3 archivos de Empresa Uno SA (02/2026 sin libro presentado)",
            "Se descargaron 2/2 archivos de Empresa Uno SA (02/2026 sin libro presentado)",
            "Se descargaron 0/4 archivos de Empresa Dos SRL (Ventas 01/2026: error)",
            "Se descargaron 1/4 archivos de Empresa Dos SRL (Ventas 01/2026: error)",
        ]
        assert len(events) == 7
        assert all(edit[1] is not None for edit in state.progress_message.edits)

    asyncio.run(scenario())


def test_batch_file_progress_only_edits_existing_message():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", progress_message=FakeMessage(),
                          selected_clients=[_scope_item(nombre="Empresa Uno SA")], period_from="2026-01",
                          period_to="2026-01", batch_months=1)
        for book in ("ventas", "compras"):
            await flow._batch_progress_event(state, "file", {
                "id_contribuyente": 1, "periodo": "2026-01", "tipo": book,
                "estado": "descargado",
            })
        assert [edit[0] for edit in state.progress_message.edits] == [
            "Se descargaron 1/2 archivos de Empresa Uno SA",
            "Se descargaron 2/2 archivos de Empresa Uno SA",
        ]
        adapter._bot.send_message.assert_not_awaited()

    asyncio.run(scenario())


def test_batch_progress_groups_months_without_book_and_keeps_error_visible():
    async def scenario():
        flow = PortalIvaFlow()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", progress_message=FakeMessage(),
                          selected_clients=[_scope_item(nombre="Empresa Uno SA")],
                          period_from="2026-01", period_to="2026-05", batch_months=5)
        for month in ("01", "02", "03"):
            for book in ("ventas", "compras"):
                await flow._batch_progress_event(state, "file", {
                    "id_contribuyente": 1, "periodo": f"2026-{month}",
                    "tipo": book, "estado": "sin_libro",
                })
        await flow._batch_progress_event(state, "file", {
            "id_contribuyente": 1, "periodo": "2026-04",
            "tipo": "ventas", "estado": "error",
        })
        await flow._batch_progress_event(state, "file", {
            "id_contribuyente": 1, "periodo": "2026-04",
            "tipo": "compras", "estado": "descargado",
        })
        assert state.progress_message.edits[-1][0] == (
            "Se descargaron 1/4 archivos de Empresa Uno SA "
            "(sin libro presentado: 01, 02 y 03/2026; Ventas 04/2026: error)"
        )

    asyncio.run(scenario())


def test_captcha_is_sent_and_reply_resumes_same_process(tmp_path):
    async def scenario():
        nonce = "a" * 16
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        captcha = run_dir / f"captcha-{nonce}.png"
        captcha.write_bytes(b"valid-image-bytes")
        captcha.chmod(0o600)

        stdout = asyncio.StreamReader()
        stdout.feed_data(
            f'PORTAL_IVA_CAPTCHA:{{"nonce":"{nonce}","path":"{captcha}"}}\n'.encode()
        )
        stderr = asyncio.StreamReader()
        proc = SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            stdin=FakeStdin(),
            returncode=0,
            wait=AsyncMock(return_value=0),
        )
        adapter = FakeAdapter()
        state = FlowState(
            user_id="7",
            nonce="b" * 10,
            stage="running",
            progress_message=FakeMessage(),
        )
        flow = PortalIvaFlow(captcha_root=tmp_path)
        key = flow._key("10", None, "7")
        flow.states[key] = state

        communication = asyncio.create_task(
            flow._communicate_with_progress(
                proc, state, adapter=adapter, chat_id="10", thread_id=None,
            )
        )
        for _ in range(100):
            if state.stage == "captcha":
                break
            await asyncio.sleep(0)
        assert state.stage == "captcha"
        adapter._bot.send_photo.assert_awaited_once()
        assert state.progress_message.edits == []
        assert await flow.text(adapter, _message("AbC123")) is True

        stdout.feed_data(b'{"ok":true,"etapa":"completado"}\n')
        stdout.feed_eof()
        stderr.feed_eof()
        raw, captured_stderr = await communication

        assert raw == b'{"ok":true,"etapa":"completado"}\n'
        assert captured_stderr == b""
        assert json.loads(proc.stdin.data) == {"nonce": nonce, "solution": "AbC123"}
        assert state.stage == "running"
        assert state.captcha_response is None

    asyncio.run(scenario())


def test_invalid_captcha_reply_does_not_release_waiter():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="captcha")
        state.captcha_response = asyncio.get_running_loop().create_future()
        flow.states[flow._key("10", None, "7")] = state

        assert await flow.text(adapter, _message("abc 123")) is True

        assert not state.captcha_response.done()
        assert "sólo con esos caracteres" in adapter._bot.send_message.await_args.kwargs["text"]

    asyncio.run(scenario())


def test_captcha_path_rejects_escape_and_symlink(tmp_path):
    flow = PortalIvaFlow(captcha_root=tmp_path)
    nonce = "a" * 16
    outside = tmp_path.parent / f"captcha-{nonce}.png"
    outside.write_bytes(b"image")
    outside.chmod(0o600)
    with pytest.raises(RuntimeError, match="CAPTCHA_PATH_INVALID"):
        flow._validated_captcha_path(str(outside), nonce)

    target = tmp_path / "target.png"
    target.write_bytes(b"image")
    target.chmod(0o600)
    link = tmp_path / f"captcha-{nonce}.png"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="CAPTCHA_PATH_INVALID"):
        flow._validated_captcha_path(str(link), nonce)


def test_success_delivers_both_csvs_and_updates_same_message(
    monkeypatch, tmp_path, verify_representation_mock,
):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit=VALID_CUIT, period="2026-08", progress_message=FakeMessage(), scope_item=_scope_item(slug="cliente"))
        payload = json.dumps(_result(tmp_path, state)).encode()
        flow._by_id = lambda _ident, _uid: [_scope_item(slug="cliente")]
        flow._query = _scope_query
        flow._acquire_execution_lock = lambda _key: None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess(payload)))
        await flow._run(adapter, "10", None, "10::7", state)
        assert adapter.send_document.await_count == 2
        sent_names = [call.kwargs["file_name"] for call in adapter.send_document.await_args_list]
        assert sent_names == ["cliente-portal-iva-ventas-2026-08.csv", "cliente-portal-iva-compras-2026-08.csv"]
        verify_representation_mock.assert_awaited_once()
        assert state.progress_message.edits[-1][0] == (
            "Portal IVA completado.\n"
            "Ventas: sin comprobantes para el período.\n"
            "Compras: sin comprobantes para el período."
        )
    asyncio.run(scenario())


def test_completion_translates_empty_book_and_keeps_warning_codes_internal(monkeypatch, tmp_path, caplog):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit=VALID_CUIT, period="2026-08", progress_message=FakeMessage(), scope_item=_scope_item(slug="cliente"))
        result = _result(tmp_path, state)
        result["archivos"][1]["filas"] = 34
        result["advertencias"] = [
            "IMPORTACION_NUMEROS_NO_PARSEADOS_ventas",
            "IMPORTACION_NUMEROS_NO_PARSEADOS_compras",
            "CSV_SIN_FILAS_ventas",
        ]
        flow._by_id = lambda _ident, _uid: [_scope_item(slug="cliente")]
        flow._query = _scope_query
        flow._acquire_execution_lock = lambda _key: None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess(json.dumps(result).encode())))

        await flow._run(adapter, "10", None, "10::7", state)

        final_text = state.progress_message.edits[-1][0]
        assert final_text == (
            "Portal IVA completado.\n"
            "Ventas: sin comprobantes para el período.\n"
            "Compras: 34 comprobantes; archivo enviado."
        )
        assert "IMPORTACION_NUMEROS_NO_PARSEADOS" not in final_text
        assert "CSV_SIN_FILAS" not in final_text
        assert "IMPORTACION_NUMEROS_NO_PARSEADOS_ventas" in caplog.text

    asyncio.run(scenario())


def test_progress_messages_match_presented_route_stages():
    assert PortalIvaFlow._progress_text("buscar_presentado") == "Buscando presentación…"
    assert PortalIvaFlow._progress_text("descargar_ventas") == "Descargando Libro IVA Ventas…"
    assert PortalIvaFlow._progress_text("descargar_compras") == "Descargando Libro IVA Compras…"
    assert PortalIvaFlow._progress_text("validar_archivos") == "Validando archivos…"


def test_remote_filename_mismatch_is_logged_without_aborting_delivery(monkeypatch, tmp_path, caplog):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        adapter = FakeAdapter()
        adapter.send_document = AsyncMock(return_value=SimpleNamespace(success=True, delivered_filename="otro.csv"))
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit=VALID_CUIT, period="2026-08", progress_message=FakeMessage(), scope_item=_scope_item(slug="cliente"))
        flow._by_id = lambda _ident, _uid: [_scope_item(slug="cliente")]
        flow._query = _scope_query
        flow._acquire_execution_lock = lambda _key: None
        payload = json.dumps(_result(tmp_path, state)).encode()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess(payload)))
        await flow._run(adapter, "10", None, "10::7", state)
        assert adapter.send_document.await_count == 2
        assert state.progress_message.edits[-1][0] == (
            "Portal IVA completado.\n"
            "Ventas: sin comprobantes para el período.\n"
            "Compras: sin comprobantes para el período."
        )
        assert caplog.text.count("DELIVERY_FILENAME_MISMATCH") == 2
        assert "expected='cliente-portal-iva-ventas-2026-08.csv' delivered='otro.csv'" in caplog.text

    asyncio.run(scenario())


def test_operation_callbacks_keep_shared_contributor_period_lock():
    async def scenario():
        flow = PortalIvaFlow()
        flow._query = _scope_query
        adapter = FakeAdapter()
        generate = _query("pi:generar")
        assert await flow.callback(adapter, generate, "pi:generar", "10", None, "7")
        state = flow.states[flow._key("10", None, "7")]
        assert state.operation == "generar"
        key = flow._key("10", None, "7")
        flow.tasks[key] = asyncio.current_task()
        download = _query("pi:descargar")
        assert await flow.callback(adapter, download, "pi:descargar", "10", None, "7")
        download.answer.assert_awaited_once_with("Ya hay una descarga Portal IVA en curso.")

    asyncio.run(scenario())


def test_incomplete_output_and_exit_one_do_not_deliver(monkeypatch, tmp_path):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit=VALID_CUIT, period="2026-08", progress_message=FakeMessage(), scope_item=_scope_item(slug="cliente"))
        blocked = {"ok": False, "etapa": "login", "motivo": "CREDENCIAL_RECHAZADA"}
        flow._by_id = lambda _ident, _uid: [_scope_item(slug="cliente")]
        flow._query = _scope_query
        flow._acquire_execution_lock = lambda _key: None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess(json.dumps(blocked).encode(), returncode=1)))
        await flow._run(adapter, "10", None, "10::7", state)
        assert adapter.send_document.await_count == 0
        assert "rechazó la credencial" in state.progress_message.edits[-1][0]
    asyncio.run(scenario())


def test_terminal_failure_sends_a_new_visible_message(monkeypatch, tmp_path):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        adapter = FakeAdapter()
        state = FlowState(
            user_id="7", nonce="a" * 10, stage="running", contributor_id=1,
            slug="cliente", cuit=VALID_CUIT, period="2026-08",
            progress_message=FakeMessage(), operation="descargar-presentados",
            scope_item=_scope_item(slug="cliente"),
        )
        blocked = {"ok": False, "motivo": "PERIODO_NO_PRESENTADO_2026-08"}
        flow._by_id = lambda _ident, _uid: [_scope_item(slug="cliente")]
        flow._query = _scope_query
        flow._acquire_execution_lock = lambda _key: None
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=FakeProcess(json.dumps(blocked).encode(), returncode=1)),
        )

        await flow._run(adapter, "10", None, "10::7", state)

        assert adapter.send_document.await_count == 0
        expected = (
            "Descargar CSV presentados: el período 08/2026 no figura como presentado en ARCA. "
            "Si todavía no fue presentado, usá “Generar CSV de período nuevo”."
        )
        assert state.progress_message.edits[-1][0] == expected
        assert adapter._bot.send_message.await_args.kwargs["text"] == expected

    asyncio.run(scenario())


def test_cancel_kills_process_group(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        state = FlowState(user_id="7", nonce="a" * 10, stage="captcha", progress_message=FakeMessage())
        state.captcha_response = asyncio.get_running_loop().create_future()
        flow.states[key] = state
        flow.processes[key] = FakeProcess(b"", returncode=None)
        killed = []
        monkeypatch.setattr("plugins.platforms.telegram.portal_iva_flow.os.killpg", lambda pid, sig: killed.append((pid, sig)))
        query = _query("pi:cancel:" + "a" * 10)
        assert await flow.callback(adapter, query, query.data, "10", None, "7")
        assert killed and killed[0][0] == 4242
        assert state.cancelled is True
        assert state.captcha_response.result() is None
    asyncio.run(scenario())


def test_ticker_keeps_one_progress_message(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", progress_message=FakeMessage())

        ticks = 0

        async def one_tick(_seconds):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                state.cancelled = True

        monkeypatch.setattr(asyncio, "sleep", one_tick)
        await flow._ticker(state, 0)
        assert len(state.progress_message.edits) == 1

    asyncio.run(scenario())


def test_cancel_before_spawn_does_not_start_executor(monkeypatch, tmp_path):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", slug="cliente", cuit="20123456789", period="2026-08", cancelled=True)
        spawn = AsyncMock()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        await flow._run(FakeAdapter(), "10", None, "10::7", state)
        spawn.assert_not_awaited()

    asyncio.run(scenario())


def test_release_clears_delivering_state():
    async def scenario():
        flow = PortalIvaFlow()
        key = flow._key("10", None, "7")
        flow.states[key] = FlowState(user_id="7", nonce="a" * 10, stage="delivering")
        task = SimpleNamespace(result=lambda: None)
        flow.tasks[key] = task
        flow._release(key, task)
        assert key not in flow.states
        assert key not in flow.tasks

    asyncio.run(scenario())


def test_release_unblocks_pending_captcha():
    async def scenario():
        flow = PortalIvaFlow()
        key = flow._key("10", None, "7")
        state = FlowState(user_id="7", nonce="a" * 10, stage="captcha")
        state.captcha_response = asyncio.get_running_loop().create_future()
        flow.states[key] = state
        task = SimpleNamespace(result=lambda: None)
        flow.tasks[key] = task

        flow._release(key, task)

        assert state.captcha_response.result() is None
        assert key not in flow.states
        assert key not in flow.tasks

    asyncio.run(scenario())


def test_own_relation_is_explained_instead_of_a_mute_failure():
    # 2026-09-15: pedir al propio titular agotaba dos esperas de 90 s y
    # terminaba en el mensaje genérico. Portal IVA no lista la identidad activa.
    assert PortalIvaFlow._error_message(
        {"motivo": "TITULAR_ES_EL_REPRESENTADO_20218332650"}, "fallback", "generar"
    ) == (
        "Generar CSV de período nuevo: el contribuyente elegido es el titular de la "
        "clave de ARCA, así que no hay representación que usar. Portal IVA no deja "
        "representarse a uno mismo, y la portada avisa que ese CUIT no tiene activa "
        "la caracterización de IVA. Elegí un contribuyente representado por ese titular."
    )


def test_unlisted_represented_taxpayer_points_at_arca_not_at_the_bot():
    assert PortalIvaFlow._error_message(
        {"motivo": "REPRESENTADO_NO_LISTADO_20218332650"},
        "fallback",
        "descargar-presentados",
    ) == (
        "Descargar CSV presentados: ARCA no lista a ese contribuyente entre los "
        "representados por el titular de la clave. Revisá la representación en ARCA."
    )


def test_unavailable_period_names_the_periods_the_portal_does_accept():
    assert PortalIvaFlow._error_message(
        {"motivo": "PERIODO_NO_DISPONIBLE_2026-07_OFRECE_202609_202608"},
        "fallback",
        "generar",
    ) == (
        "Generar CSV de período nuevo: el período 07/2026 no está disponible. "
        "ARCA ofrece 09/2026, 08/2026 para declaración nueva."
    )


def test_unavailable_period_without_offered_list_keeps_the_previous_message():
    # El ejecutor viejo, o un portal que no expone opciones, no debe romper.
    assert PortalIvaFlow._error_message(
        {"motivo": "PERIODO_NO_DISPONIBLE_2026-05"}, "fallback", "generar"
    ) == "Generar CSV de período nuevo: el período no está disponible para esta operación."


def test_batch_selects_multiple_contributors_then_asks_for_month_range(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="client", operation="descargar-lote")
        flow._by_id = lambda ident, _uid: [_scope_item(id=ident)]
        key = flow._key("10", None, "7")
        flow.states[key] = state
        await flow._select(adapter, "10", None, state, _scope_item())
        await flow._select(adapter, "10", None, state, _scope_item(id=2, slug="dos", nombre="Dos"))
        assert [item["id"] for item in state.selected_clients] == [1, 2]
        assert state.stage == "batch_clients"
        assert await flow.text(adapter, _message("LISTO"))
        assert state.stage == "batch_from"

        def fake_task(coro):
            coro.close()
            return SimpleNamespace(add_done_callback=lambda _callback: None)

        monkeypatch.setattr(asyncio, "create_task", fake_task)
        assert await flow.text(adapter, _message("2026-05"))
        assert state.stage == "batch_from"
        assert await flow.text(adapter, _message("05/2026"))
        assert state.stage == "batch_to"
        assert await flow.text(adapter, _message("06/2026"))
        assert state.stage == "running"
        assert state.period_from == "2026-05" and state.period_to == "2026-06"

    asyncio.run(scenario())


def test_batch_search_hides_candidates_without_verified_access():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="client", operation="descargar-lote")
        flow.states[flow._key("10", None, "7")] = state
        flow._search = lambda _term, _user: [
            _scope_item(id=1, access_id=10),
            _scope_item(id=2, nombre="Sin verificar", slug="sin-verificar", access_id=None),
        ]
        assert await flow.text(adapter, _message("cliente"))
        assert set(state.candidates) == {1}
        assert state.selected_clients[0]["id"] == 1

    asyncio.run(scenario())


def test_batch_rejects_contributors_from_another_study():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="batch_clients", operation="descargar-lote",
                          selected_clients=[_scope_item(study_id=1)])
        await flow._select(adapter, "10", None, state, _scope_item(id=2, study_id=2, slug="dos", nombre="Dos"))
        assert len(state.selected_clients) == 1
        assert "mismo estudio" in adapter._bot.send_message.await_args.kwargs["text"]

    asyncio.run(scenario())


def test_batch_accepts_contributors_with_another_fiscal_holder():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="batch_clients", operation="descargar-lote",
                          selected_clients=[_scope_item(study_id=1, representative_id=1)])
        await flow._select(
            adapter, "10", None, state,
            _scope_item(id=2, study_id=1, representative_id=2, slug="dos", nombre="Dos"),
        )
        assert len(state.selected_clients) == 2
        assert "Agregado: Dos" in adapter._bot.send_message.await_args.kwargs["text"]

    asyncio.run(scenario())


def test_batch_revalidates_execute_permission_immediately_before_running():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        queries = []

        def query(sql):
            queries.append(sql)
            if "fn_actor_telegram_vinculado" in sql:
                return [{"linked": True}]
            if "fn_actor_telegram_permiso" in sql:
                return [{"allowed": False}]
            return []

        flow._query = query
        state = FlowState(
            user_id="7", nonce="a" * 10, stage="running",
            operation="descargar-lote", selected_clients=[_scope_item()],
            period_from="2026-05", period_to="2026-06",
            progress_message=FakeMessage(),
        )
        await flow._run_batch(adapter, "10", None, "key", state)
        assert any("fn_actor_telegram_permiso" in sql for sql in queries)
        assert state.progress_message.edits[-1][0].startswith("No pude completar el lote")

    asyncio.run(scenario())


def test_batch_revalidation_replaces_stale_binding_before_command(monkeypatch, tmp_path):
    async def scenario():
        flow = PortalIvaFlow(batch_result_root=tmp_path / "results")
        flow._query = _scope_query
        stale = _scope_item(access_id=10, representative_id=20)
        fresh = _scope_item(access_id=88, representative_id=77)
        flow._by_id = lambda _item_id, _user_id: [fresh]
        captured = {}
        flow._batch_command = lambda state, _run_id, _result_file: captured.setdefault(
            "selected", [(item["access_id"], item["representative_id"])
                         for item in state.selected_clients]) or ["fake"]

        class Proc:
            returncode = 0
            pid = 4321
        async def subprocess_exec(*_args, **_kwargs): return Proc()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_exec)
        async def communicate(*_args, **_kwargs):
            return (b"diagnostico previo\nsalida que no es JSON\n", b"")
        flow._communicate_with_progress = communicate
        flow._read_batch_result = lambda _path: {
            "ok": True, "casos": [], "xlsx": [],
            "resumen": {"completados": 0, "total": 0},
        }
        async def ticker(*_args): await asyncio.sleep(60)
        flow._ticker = ticker
        flow._batch_deliverables = lambda _result: []
        flow._batch_evidence = lambda _result: []
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", selected_clients=[stale],
                          period_from="2026-05", period_to="2026-05",
                          progress_message=FakeMessage())
        await flow._run_batch(FakeAdapter(), "10", None, "key", state)
        assert captured["selected"] == [(88, 77)]
        assert [(item["access_id"], item["representative_id"])
                for item in state.selected_clients] == [(88, 77)]

    asyncio.run(scenario())


def test_batch_command_is_shell_free_and_contains_every_selected_slug(tmp_path):
    executor = tmp_path / "portal_iva.py"
    executor.touch()
    batch_executor = tmp_path / "portal_iva_lote.py"
    batch_executor.touch()
    uv = tmp_path / "uv"
    uv.touch()
    root = tmp_path / "clientes"
    root.mkdir()
    flow = PortalIvaFlow(executor=executor, uv=uv, clients_root=root)
    state = FlowState(user_id="7", nonce="a" * 10, stage="running", operation="descargar-lote",
                      selected_clients=[{"slug": "uno", "access_id": 10, "representative_id": 20},
                                        {"slug": "dos", "access_id": 11, "representative_id": 21}],
                      period_from="2026-05", period_to="2026-06")
    result_file = tmp_path / "result.json"
    assert flow._batch_command(state, 321, result_file) == [
        str(uv), "run", "--with", "selenium", "--with", "openpyxl", "xvfb-run", "-a",
        "python3", str(batch_executor),
        "--caso", "uno:2026-05:10:20", "--caso", "uno:2026-06:10:20",
        "--caso", "dos:2026-05:11:21", "--caso", "dos:2026-06:11:21",
        "--captcha-stdin", "--result-file", str(result_file),
        "--history-run-id", "321",
    ]


def test_batch_result_file_is_authoritative_and_rejects_insecure_metadata(tmp_path):
    root = tmp_path / "results"
    root.mkdir(mode=0o700)
    flow = PortalIvaFlow(batch_result_root=root)
    directory, target = flow._new_batch_result_target()
    target.write_text('{"ok":true,"id_lote":51}\n', encoding="utf-8")
    target.chmod(0o600)
    assert flow._read_batch_result(target) == {"ok": True, "id_lote": 51}
    target.chmod(0o640)
    with pytest.raises(RuntimeError, match="RESULT_FILE_INVALID"):
        flow._read_batch_result(target)
    shutil.rmtree(directory)


def test_failed_telegram_batch_is_registered_for_study_notification():
    flow = PortalIvaFlow(
        query_connection=lambda: (_ for _ in ()).throw(AssertionError("lookup must stay read-only")),
        write_connection=lambda: ([], {}),
    )
    captured = []
    flow._write_query = lambda sql: captured.append(sql) or [51]
    flow._register_terminal_batch(
        321,
        [{"id": 1}, {"id": 2}],
        ["2026-05", "2026-06"],
        "fallido",
        "lote_no_completado",
    )
    assert "fn_portal_iva_lote_registrar_telegram" in captured[0]
    import base64
    encoded = captured[0].split("decode('", 1)[1].split("'", 1)[0]
    payload = json.loads(base64.b64decode(encoded))
    assert len(payload["casos"]) == 4
    assert {case["estado"] for case in payload["casos"]} == {"fallido"}


def test_terminal_batch_uses_verification_connection(monkeypatch):
    lookup = lambda: (["lookup"], {"PGAPPNAME": "lookup"})
    verification = lambda: (["verification"], {"PGAPPNAME": "verification"})
    flow = PortalIvaFlow(query_connection=lookup, write_connection=verification)
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="51\n", stderr="")

    monkeypatch.setattr("subprocess.run", run)
    flow._register_terminal_batch(
        321, [{"id": 1}], ["2026-05"], "fallido", "lote_no_completado"
    )
    assert seen[0][0][0] == "verification"
    assert seen[0][1]["PGAPPNAME"] == "verification"


def test_batch_deliverables_rejects_hash_mismatch_and_accepts_csv_xlsx_and_f2083(tmp_path):
    import hashlib
    root = tmp_path / "clientes"
    folder = "estudios/1/20123456786/2026/05/arca"
    paths = {}
    for name, content in (("uno-portal-iva-ventas-2026-05.csv", b"cabecera\n"),
                          ("uno-portal-iva-libros-2026-05-a-2026-05.xlsx", b"PK synthetic"),
                          ("uno-portal-iva-f2083-2026-05.pdf", b"%PDF-1.7\nsynthetic"),
                          ("uno-portal-iva-compras-2026-06.csv", b"cabecera\n")):
        path = root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        paths[name] = (path, f"{folder}/{name}", hashlib.sha256(content).hexdigest())
    csv_path, csv_ruta, csv_sha = paths["uno-portal-iva-ventas-2026-05.csv"]
    xlsx_path, xlsx_ruta, xlsx_sha = paths["uno-portal-iva-libros-2026-05-a-2026-05.xlsx"]
    pdf_path, pdf_ruta, pdf_sha = paths["uno-portal-iva-f2083-2026-05.pdf"]
    kept_path, kept_ruta, kept_sha = paths["uno-portal-iva-compras-2026-06.csv"]
    flow = PortalIvaFlow(clients_root=root)
    result = {
        "casos": [
            {"ok": True, "cliente": "uno", "periodo": "2026-05", "archivos": [{
                "entregable_path": str(csv_path), "entregable_ruta_clientes": csv_ruta,
                "entregable_sha256": csv_sha, "filas": 1, "libro": "ventas",
            }], "f2083": {"estado": "disponible", "nombre": pdf_path.name, "ruta": str(pdf_path),
                          "ruta_clientes": pdf_ruta, "id_archivo": 3, "sha256": pdf_sha}},
            # Caso fallido: lo ya guardado se entrega igual (Ágora #115).
            {"ok": False, "cliente": "uno", "periodo": "2026-06", "motivo_codigo": "cuota_insuficiente",
             "archivos": [{"entregable_path": str(kept_path), "entregable_ruta_clientes": kept_ruta,
                           "entregable_sha256": kept_sha, "filas": 0, "libro": "compras"}],
             "f2083": {"estado": "no_disponible"}},
        ],
        "xlsx": [{"cliente": "uno", "ruta": str(xlsx_path), "ruta_clientes": xlsx_ruta,
                  "id_archivo": 4, "sha256": xlsx_sha}],
    }
    delivered = flow._batch_deliverables(result)
    assert [path for path, _, _ in delivered] == [csv_path, pdf_path, kept_path, xlsx_path]
    result["xlsx"][0]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="PORTAL_IVA_DELIVERY_HASH_INVALID"):
        flow._batch_deliverables(result)
    result["xlsx"][0].update(sha256=xlsx_sha, ruta_clientes="estudios/1/20123456786/2026/05/arca/otro.xlsx")
    with pytest.raises(RuntimeError, match="PORTAL_IVA_DELIVERY_PATH_INVALID"):
        flow._batch_deliverables(result)


def test_batch_reports_consolidated_workbook_errors():
    lines = PortalIvaFlow._batch_xlsx_errors({"xlsx_errores": [
        {"cliente": "uno", "id_contribuyente": 1, "motivo": "x", "estado": "error",
         "motivo_codigo": "cuota_insuficiente",
         "motivo_texto": "El estudio no tiene espacio suficiente. Liberá archivos desde Documentos o pedí ampliar la cuota.",
         "accion_sugerida": None},
        {"cliente": "dos", "id_contribuyente": 2, "motivo": "x", "estado": "error",
         "motivo_codigo": "documentos_no_guardado", "motivo_texto": "No se pudo guardar el consolidado.",
         "accion_sugerida": "Reintentá más tarde."},
    ]})
    assert lines == [
        "• uno · consolidado: El estudio no tiene espacio suficiente. Liberá archivos desde Documentos o pedí ampliar la cuota.",
        "• dos · consolidado: No se pudo guardar el consolidado. Reintentá más tarde.",
    ]


def _estudios_file(root: Path, cuit: str, name: str, content: bytes = b"", size: int | None = None) -> Path:
    path = root / "estudios" / "1" / cuit / "2026" / "01" / "arca" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if size is None:
        path.write_bytes(content)
    else:
        with path.open("wb") as handle:
            handle.truncate(size)
    return path


def test_batch_archive_contains_files_once_without_cuit_in_names(tmp_path):
    root = tmp_path / "clientes"
    files = []
    for slug, cuit in (("uno", "20123456786"), ("dos", "20987654321")):
        for name in ("ventas.csv", "compras.csv"):
            path = _estudios_file(root, cuit, f"{slug}-{name}", (slug + name).encode())
            files.append((path, 1, f"{slug} · 2026-01 · {name}"))
    evidence_root = tmp_path / "runs"
    evidence_root.mkdir()
    capture = evidence_root / "capture.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\n")
    flow = PortalIvaFlow(clients_root=root, captcha_root=evidence_root)
    staging, archive = flow._stage_batch_archive(
        files, evidence=[(capture, "captura-arca-uno-2026-01.png")]
    )
    try:
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
            assert len(names) == len(files) + 1 == len(set(names))
            assert all("20123456786" not in name and "20987654321" not in name for name in names)
            assert package.read("uno/2026/01/arca/uno-ventas.csv") == b"unoventas.csv"
            assert package.read("evidencia/captura-arca-uno-2026-01.png") == b"\x89PNG\r\n\x1a\n"
        assert all(path.exists() for path, _, _ in files)
        assert capture.exists()
    finally:
        shutil.rmtree(staging)
    # Fuera de estudios/ (estructura vieja) no se empaqueta.
    old = root / "uno" / "20123456786" / "arca" / "2026" / "01" / "consultas" / "ventas.csv"
    old.parent.mkdir(parents=True)
    old.write_text("x")
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._stage_batch_archive([(old, 1, "uno · 2026-01 · ventas")])


def test_batch_120_mb_is_split_into_three_valid_zips(tmp_path):
    root = tmp_path / "clientes"
    files = []
    for slug in ("uno", "dos", "tres"):
        path = _estudios_file(root, VALID_CUIT, f"{slug}.csv", size=40_000_000)
        files.append((path, 1, f"{slug} · 2026-01 · ventas"))
    flow = PortalIvaFlow(clients_root=root)
    archives, skipped = flow._stage_batch_archives(files, "libros-iva-202601-a-202601.zip", [])
    try:
        assert skipped == []
        assert len(archives) == 3
        for index, (_, archive) in enumerate(archives, 1):
            assert archive.name == f"libros-iva-202601-a-202601-parte-{index}-de-3.zip"
            assert archive.stat().st_size <= 45_000_000
            with zipfile.ZipFile(archive) as package:
                assert package.testzip() is None
                assert len(package.namelist()) == 1
    finally:
        for staging, _ in archives:
            shutil.rmtree(staging)


def test_batch_60_mb_file_is_omitted_with_notice_not_error(monkeypatch, tmp_path):
    async def scenario():
        root = tmp_path / "clientes"
        huge = _estudios_file(root, VALID_CUIT, "ventas.csv", size=60_000_000)
        flow = PortalIvaFlow(clients_root=root, batch_result_root=tmp_path / "results")
        flow._query = _scope_query
        flow._by_id = lambda _item_id, _user_id: [_scope_item()]
        flow._batch_command = lambda *_args: ["fake"]
        flow._batch_deliverables = lambda _result: [(huge, 1, "uno · 2026-01 · ventas")]
        flow._batch_evidence = lambda _result: []
        flow._read_batch_result = lambda _path: {
            "ok": True, "casos": [], "xlsx": [],
            "resumen": {"completados": 1, "total": 1},
        }

        class Proc:
            returncode = 0
            pid = 4321

        async def subprocess_exec(*_args, **_kwargs):
            return Proc()

        async def communicate(*_args, **_kwargs):
            return b"", b""

        monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_exec)
        flow._communicate_with_progress = communicate
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", selected_clients=[_scope_item()],
                          period_from="2026-01", period_to="2026-01",
                          progress_message=FakeMessage())
        adapter = FakeAdapter()
        await flow._run_batch(adapter, "10", None, "key", state)
        adapter.send_document.assert_not_awaited()
        assert "quedaron en Documentos y en el panel" in state.progress_message.edits[-1][0]
        assert huge.exists()

    asyncio.run(scenario())


def test_batch_delivery_sends_one_zip_instead_of_individual_files(monkeypatch, tmp_path):
    async def scenario():
        root = tmp_path / "clientes"
        files = []
        for name in ("ventas.csv", "compras.csv", "f2083.pdf"):
            path = _estudios_file(root, VALID_CUIT, name, name.encode())
            files.append((path, 1, f"uno · 2026-01 · {name}"))
        flow = PortalIvaFlow(clients_root=root, batch_result_root=tmp_path / "results")
        flow._query = _scope_query
        flow._by_id = lambda _item_id, _user_id: [_scope_item()]
        flow._batch_command = lambda *_args: ["fake"]
        flow._batch_deliverables = lambda _result: files
        flow._batch_evidence = lambda _result: []
        flow._read_batch_result = lambda _path: {
            "ok": True, "casos": [], "xlsx": [],
            "resumen": {"completados": 1, "total": 1},
        }

        class Proc:
            returncode = 0
            pid = 4321

        async def subprocess_exec(*_args, **_kwargs):
            return Proc()

        async def communicate(*_args, **_kwargs):
            return b"", b""

        async def ticker(*_args, **_kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_exec)
        flow._communicate_with_progress = communicate
        flow._ticker = ticker
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", selected_clients=[_scope_item()],
                          period_from="2026-01", period_to="2026-01",
                          progress_message=FakeMessage())
        adapter = FakeAdapter()
        await flow._run_batch(adapter, "10", None, "key", state)

        adapter.send_document.assert_awaited_once()
        call = adapter.send_document.await_args.kwargs
        assert call["file_name"].endswith(".zip")
        assert "3 archivos" in call["caption"]
        assert all(path.exists() for path, _, _ in files)

    asyncio.run(scenario())


def test_batch_without_presented_periods_has_no_deliverables(tmp_path):
    root = tmp_path / "clientes"
    root.mkdir()
    flow = PortalIvaFlow(clients_root=root)
    result = {
        "casos": [{"ok": False, "cliente": "uno", "periodo": "2026-05",
                   "estado": "no_disponible", "archivos": [], "f2083": None}],
        "xlsx": [],
    }
    assert flow._batch_deliverables(result) == []


def test_batch_does_not_treat_explicitly_missing_f2083_as_a_file(tmp_path):
    root = tmp_path / "clientes"
    root.mkdir()
    flow = PortalIvaFlow(clients_root=root)
    result = {"casos": [{"ok": True, "cliente": "uno", "periodo": "2026-05",
                          "archivos": [], "f2083": {"estado": "no_disponible"}}], "xlsx": []}
    assert flow._batch_deliverables(result) == []


# ------------------------------------------- Ágora #115: contrato final de portal_iva.py

MENSAJE_CUOTA = "El estudio no tiene espacio suficiente. Liberá archivos desde Documentos o pedí ampliar la cuota."


class FakeHistory:
    def __init__(self):
        self.finished = []

    async def start(self, **_kwargs):
        return SimpleNamespace(run_id=1, item_id=2)

    async def prepare_items(self, _run, references):
        return [{"id_item": index + 10, "referencia": ref} for index, ref in enumerate(references)]

    async def finish_item(self, _run, item_id, **kwargs):
        self.finished.append((item_id, kwargs))

    async def close(self, _run):
        return "completada"


def _single_flow(tmp_path, history=None):
    flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv",
                         clients_root=tmp_path, history=history)
    flow.executor.touch(); flow.uv.touch()
    flow._by_id = lambda _ident, _uid: [_scope_item(slug="cliente")]
    flow._query = _scope_query
    flow._acquire_execution_lock = lambda _key: None
    state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente",
                      cuit=VALID_CUIT, period="2026-08", progress_message=FakeMessage(),
                      scope_item=_scope_item(slug="cliente"))
    return flow, state


def test_single_history_registers_the_documents_route(monkeypatch, tmp_path, verify_representation_mock):
    async def scenario():
        history = FakeHistory()
        flow, state = _single_flow(tmp_path, history)
        result = _result(tmp_path, state)
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=FakeProcess(json.dumps(result).encode())))
        await flow._run(FakeAdapter(), "10", None, "10::7", state)
        relatives = [kwargs["output_relative"] for _, kwargs in history.finished]
        assert relatives == [record["entregable_ruta_clientes"] for record in result["archivos"]]
        assert all(kwargs["state"] == "completado" for _, kwargs in history.finished)
    asyncio.run(scenario())


def test_single_quota_shows_contabot_message_and_closes_failed(monkeypatch, tmp_path):
    async def scenario():
        history = FakeHistory()
        flow, state = _single_flow(tmp_path, history)
        blocked = {"ok": False, "codigo": "cuota_insuficiente", "mensaje": MENSAJE_CUOTA,
                   "motivo": "CUOTA_INSUFICIENTE", "etapa": "cuota", "archivos_guardados": []}
        adapter = FakeAdapter()
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=FakeProcess(json.dumps(blocked).encode(), returncode=1)))
        await flow._run(adapter, "10", None, "10::7", state)
        adapter.send_document.assert_not_awaited()
        assert adapter._bot.send_message.await_args.kwargs["text"] == MENSAJE_CUOTA
        assert {kwargs["reason_code"] for _, kwargs in history.finished} == {"cuota_insuficiente"}
        assert {kwargs["state"] for _, kwargs in history.finished} == {"fallido"}
    asyncio.run(scenario())


def test_single_failure_reports_what_was_already_saved(monkeypatch, tmp_path):
    async def scenario():
        flow, state = _single_flow(tmp_path)
        saved = _result(tmp_path, state)["archivos"][:1]
        blocked = {"ok": False, "motivo": "DOCUMENTOS_NO_GUARDADO_base_no_disponible",
                   "etapa": "descargar_compras", "archivos_guardados": saved}
        adapter = FakeAdapter()
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=FakeProcess(json.dumps(blocked).encode(), returncode=1)))
        await flow._run(adapter, "10", None, "10::7", state)
        text = adapter._bot.send_message.await_args.kwargs["text"]
        assert "Quedaron guardados en Documentos: cliente-portal-iva-ventas-2026-08.csv, " \
               "cliente-portal-iva-ventas-2026-08.zip." in text
        adapter.send_document.assert_not_awaited()
    asyncio.run(scenario())


def test_single_delivers_f2083_when_reported(monkeypatch, tmp_path, verify_representation_mock):
    async def scenario():
        flow, state = _single_flow(tmp_path)
        result = _result(tmp_path, state)
        fields = _saved_fields(tmp_path, "x", f"estudios/1/{VALID_CUIT}/2026/08/arca/cliente-portal-iva-f2083-2026-08.pdf",
                               b"%PDF-1.7")
        result["f2083"] = {"estado": "disponible", "nombre": fields["x_name"], "ruta": fields["x_path"],
                           "ruta_clientes": fields["x_ruta_clientes"], "id_archivo": 7,
                           "bytes": fields["x_bytes"], "sha256": fields["x_sha256"]}
        adapter = FakeAdapter()
        monkeypatch.setattr(asyncio, "create_subprocess_exec",
                            AsyncMock(return_value=FakeProcess(json.dumps(result).encode())))
        await flow._run(adapter, "10", None, "10::7", state)
        names = [call.kwargs["file_name"] for call in adapter.send_document.await_args_list]
        assert names[-1] == "cliente-portal-iva-f2083-2026-08.pdf"
        assert state.progress_message.edits[-1][0].endswith("F.2083: archivo enviado.")
    asyncio.run(scenario())


def test_batch_summary_shows_quota_cases_and_workbook_errors(monkeypatch, tmp_path):
    async def scenario():
        root = tmp_path / "clientes"
        root.mkdir()
        flow = PortalIvaFlow(clients_root=root, batch_result_root=tmp_path / "results")
        flow._query = _scope_query
        flow._by_id = lambda _item_id, _user_id: [_scope_item()]
        flow._batch_command = lambda *_args: ["fake"]
        flow._batch_evidence = lambda _result: []
        flow._read_batch_result = lambda _path: {
            "ok": True, "xlsx": [],
            "casos": [{"ok": False, "cliente": "uno", "periodo": "2026-01", "id_contribuyente": 1,
                       "estado": "error", "motivo_codigo": "cuota_insuficiente",
                       "motivo_texto": MENSAJE_CUOTA, "archivos": [], "f2083": {"estado": "no_disponible"}}],
            "xlsx_errores": [{"cliente": "uno", "id_contribuyente": 1, "motivo": "x", "estado": "error",
                              "motivo_codigo": "cuota_insuficiente", "motivo_texto": MENSAJE_CUOTA,
                              "accion_sugerida": None}],
            "resumen": {"completados": 0, "total": 1},
        }

        class Proc:
            returncode = 0
            pid = 4321

        async def subprocess_exec(*_args, **_kwargs):
            return Proc()

        async def communicate(*_args, **_kwargs):
            return b"", b""

        monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_exec)
        flow._communicate_with_progress = communicate
        state = FlowState(user_id="7", nonce="a" * 10, stage="running",
                          operation="descargar-lote", selected_clients=[_scope_item()],
                          period_from="2026-01", period_to="2026-01",
                          progress_message=FakeMessage())
        await flow._run_batch(FakeAdapter(), "10", None, "key", state)
        text = state.progress_message.edits[-1][0]
        assert f"• uno · 2026-01: {MENSAJE_CUOTA}" in text
        assert f"• uno · consolidado: {MENSAJE_CUOTA}" in text

    asyncio.run(scenario())
