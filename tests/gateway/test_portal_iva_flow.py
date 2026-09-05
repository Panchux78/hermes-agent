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


class FakeProcess:
    def __init__(self, payload, returncode=0):
        self.payload = payload
        self.returncode = returncode
        self.pid = 4242

    async def communicate(self):
        return self.payload, b""

    async def wait(self):
        return self.returncode


class FakeAdapter:
    def __init__(self):
        self._bot = SimpleNamespace(send_message=AsyncMock(return_value=FakeMessage()))
        self.send_document = AsyncMock(return_value=SimpleNamespace(success=True))


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
        name = f"{label}.csv"
        (target / name).write_text("cabecera\n", encoding="utf-8")
        files.append({"libro": label, "entregable_name": name, "filas": 0})
    return {"ok": True, "etapa": "completado" if complete else "resolver", "archivos": files, "advertencias": ["CSV_SIN_FILAS_ventas"]}


def test_command_is_exact_and_shell_free(tmp_path):
    flow = PortalIvaFlow(executor=tmp_path / "portal_iva.py", uv=tmp_path / "uv", clients_root=tmp_path)
    flow.executor.touch()
    flow.uv.touch()
    assert flow._command("cliente", "2026-08") == [
        str(tmp_path / "uv"), "run", "--with", "selenium", "xvfb-run", "-a",
        "python3", str(tmp_path / "portal_iva.py"), "--cliente", "cliente", "--periodo", "2026-08",
    ]


def test_period_accepts_only_year_month():
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        state = FlowState(user_id="7", nonce="a" * 10, stage="period", slug="cliente", cuit="20123456789")
        flow.states[flow._key("10", None, "7")] = state
        assert await flow.text(adapter, _message("2026-13")) is True
        adapter._bot.send_message.assert_awaited_once()
        assert state.stage == "period"
    asyncio.run(scenario())


def test_search_handles_unique_multiple_and_missing(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        flow._search = lambda term: []
        await flow.start(adapter, _query("pi:start"), "10", None, "7")
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
        start = _query("pi:start")
        assert await flow.callback(adapter, start, "pi:start", "10", None, "7")
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
    (target / "ventas.csv").unlink()
    (target / "ventas.csv").symlink_to(tmp_path / "elsewhere.csv")
    with pytest.raises(RuntimeError, match="DELIVERY_PATH"):
        flow._deliverables(state.slug, state.cuit, state.period, result)


def test_stdout_requires_one_json_object():
    assert PortalIvaFlow._parse_result(b'{"ok":true}\n') == {"ok": True}
    with pytest.raises(RuntimeError, match="STDOUT_INVALID"):
        PortalIvaFlow._parse_result(b'{"ok":true}\nnoise\n')
    with pytest.raises(RuntimeError, match="STDOUT_INVALID"):
        PortalIvaFlow._parse_result(b'not-json\n')


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
        assert "Ventas y Compras enviadas" in state.progress_message.edits[-1][0]
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


def test_cancel_kills_process_group(monkeypatch):
    async def scenario():
        flow = PortalIvaFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", progress_message=FakeMessage())
        flow.states[key] = state
        flow.processes[key] = FakeProcess(b"", returncode=None)
        killed = []
        monkeypatch.setattr("plugins.platforms.telegram.portal_iva_flow.os.killpg", lambda pid, sig: killed.append((pid, sig)))
        query = _query("pi:cancel:" + "a" * 10)
        assert await flow.callback(adapter, query, query.data, "10", None, "7")
        assert killed and killed[0][0] == 4242
        assert state.cancelled is True
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
