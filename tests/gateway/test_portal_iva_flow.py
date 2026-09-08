import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.portal_iva_flow import FlowState, PortalIvaFlow


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


def _result(root: Path, state: FlowState, *, complete=True):
    year, month = state.period.split("-")
    target = root / state.slug / state.cuit / "arca" / year / month / "consultas"
    target.mkdir(parents=True, exist_ok=True)
    files = []
    for label in ("ventas", "compras"):
        official = f"arca-{label}.csv"
        deliverable = f"cliente-portal-iva-{label}.csv"
        (target / official).write_text("cabecera\n", encoding="utf-8")
        (target / deliverable).write_text("cabecera\n", encoding="utf-8")
        files.append({"libro": label, "csv_name": official, "entregable_name": deliverable, "filas": 0})
    return {"ok": True, "etapa": "completado" if complete else "resolver", "archivos": files, "advertencias": ["CSV_SIN_FILAS_ventas"]}


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
        state = FlowState(user_id="7", nonce="a" * 10, stage="period", slug="cliente", cuit="20123456789")
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
            state = FlowState(user_id="7", nonce="a" * 10, stage="period", slug="cliente", cuit="20123456789")
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
        await flow._select(adapter, "10", None, state, {"id": 1, "nombre": "Uno", "cuit": "20123456789", "slug": "uno"})
        assert adapter._bot.send_message.await_args.kwargs["text"] == "Ingresá el período como MM/AAAA. Ejemplo: 08/2026."

    asyncio.run(scenario())


def test_flow_never_exposes_executor_period_format_to_telegram():
    source = Path("plugins/platforms/telegram/portal_iva_flow.py").read_text(encoding="utf-8")
    assert "AAAA-MM" not in source


def test_search_handles_unique_multiple_and_missing(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        flow._search = lambda term: []
        await flow.start(adapter, _query("pi:generar"), "10", None, "7", "generar")
        assert await flow.text(adapter, _message("nadie"))
        flow._search = lambda term: [{"id": 1, "nombre": "Uno", "cuit": "20123456789", "slug": "uno"}]
        assert await flow.text(adapter, _message("uno"))
        assert flow.states[flow._key("10", None, "7")].stage == "period"
        flow.states[flow._key("10", None, "7")] = FlowState(user_id="7", nonce="b" * 10, stage="client")
        flow._search = lambda term: [
            {"id": 1, "nombre": "Uno", "cuit": "20123456789", "slug": "uno"},
            {"id": 2, "nombre": "Dos", "cuit": "20987654321", "slug": "dos"},
        ]
        assert await flow.text(adapter, _message("x"))
        assert "Elegí" in adapter._bot.send_message.await_args.kwargs["text"]
    asyncio.run(scenario())


def test_callback_selection_expires_and_double_start_is_blocked():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        expired = _query("pi:select:" + "a" * 10 + ":1")
        assert await flow.callback(adapter, expired, expired.data, "10", None, "7")
        expired.answer.assert_awaited_once()
        flow.tasks[flow._key("10", None, "7")] = asyncio.current_task()
        start = _query("pi:generar")
        assert await flow.callback(adapter, start, "pi:generar", "10", None, "7")
        start.answer.assert_awaited_once_with("Ya hay una descarga Portal IVA en curso.")
    asyncio.run(scenario())


def test_delivery_rejects_traversal_and_symlink(tmp_path):
    flow = PortalIvaFlow(clients_root=tmp_path)
    state = FlowState(user_id="7", nonce="a" * 10, stage="running", slug="cliente", cuit="20123456789", period="2026-08")
    result = _result(tmp_path, state)
    result["archivos"][0]["entregable_name"] = "../ventas.csv"
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)
    result = _result(tmp_path, state)
    target = tmp_path / "cliente" / "20123456789" / "arca" / "2026" / "08" / "consultas"
    (target / "cliente-portal-iva-ventas.csv").unlink()
    (target / "cliente-portal-iva-ventas.csv").symlink_to(tmp_path / "elsewhere.csv")
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)


def test_delivery_uses_canonical_deliverable_instead_of_long_official_name(tmp_path):
    flow = PortalIvaFlow(clients_root=tmp_path)
    state = FlowState(
        user_id="7",
        nonce="a" * 10,
        stage="running",
        slug="cliente",
        cuit="20123456789",
        period="2026-08",
    )
    result = _result(tmp_path, state)
    target = tmp_path / "cliente" / "20123456789" / "arca" / "2026" / "08" / "consultas"
    official = "comprobantes_periodo_202608_ventas_20260906_1145 (montos expresados en pesos).csv"
    assert len(official) > 64
    (target / official).write_text("cabecera\n", encoding="utf-8")
    result["archivos"][0]["csv_name"] = official

    deliverables = flow._deliverables(state.slug, state.cuit, state.period, result)

    assert [path.name for path, _, _ in deliverables] == [
        "cliente-portal-iva-ventas.csv",
        "cliente-portal-iva-compras.csv",
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


def test_success_delivers_both_csvs_and_updates_same_message(monkeypatch, tmp_path):
    async def scenario():
        flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
        flow.executor.touch(); flow.uv.touch()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit="20123456789", period="2026-08", progress_message=FakeMessage())
        payload = json.dumps(_result(tmp_path, state)).encode()
        flow._by_id = lambda _ident: [{"slug": "cliente", "cuit": "20123456789"}]
        flow._acquire_execution_lock = lambda _key: None
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=FakeProcess(payload)))
        await flow._run(adapter, "10", None, "10::7", state)
        assert adapter.send_document.await_count == 2
        sent_names = [call.kwargs["file_name"] for call in adapter.send_document.await_args_list]
        assert sent_names == ["cliente-portal-iva-ventas.csv", "cliente-portal-iva-compras.csv"]
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
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit="20123456789", period="2026-08", progress_message=FakeMessage())
        result = _result(tmp_path, state)
        result["archivos"][1]["filas"] = 34
        result["advertencias"] = [
            "IMPORTACION_NUMEROS_NO_PARSEADOS_ventas",
            "IMPORTACION_NUMEROS_NO_PARSEADOS_compras",
            "CSV_SIN_FILAS_ventas",
        ]
        flow._by_id = lambda _ident: [{"slug": "cliente", "cuit": "20123456789"}]
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
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit="20123456789", period="2026-08", progress_message=FakeMessage())
        flow._by_id = lambda _ident: [{"slug": "cliente", "cuit": "20123456789"}]
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
        assert "expected='cliente-portal-iva-ventas.csv' delivered='otro.csv'" in caplog.text

    asyncio.run(scenario())


def test_operation_callbacks_keep_shared_contributor_period_lock():
    async def scenario():
        flow = PortalIvaFlow()
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
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, slug="cliente", cuit="20123456789", period="2026-08", progress_message=FakeMessage())
        blocked = {"ok": False, "etapa": "login", "motivo": "CREDENCIAL_RECHAZADA"}
        flow._by_id = lambda _ident: [{"slug": "cliente", "cuit": "20123456789"}]
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
            slug="cliente", cuit="20123456789", period="2026-08",
            progress_message=FakeMessage(), operation="descargar-presentados",
        )
        blocked = {"ok": False, "motivo": "PERIODO_NO_PRESENTADO_2026-08"}
        flow._by_id = lambda _ident: [{"slug": "cliente", "cuit": "20123456789"}]
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
