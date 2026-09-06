import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.agip_ddjj_flow import (
    AgipDdjjFlow,
    FlowState,
    is_valid_delivery_path,
    normalize_period,
    visible_cuit,
)


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeAdapter:
    def __init__(self):
        self._bot = SimpleNamespace(send_message=AsyncMock(return_value=FakeMessage()))
        self.send_document = AsyncMock(return_value=SimpleNamespace(success=True, message_id="42"))


class FakeRunningProcess:
    def __init__(self):
        self.pid = 4242
        self.returncode = None

    async def wait(self):
        return self.returncode


def _query(data):
    return SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())


def _message(text):
    return SimpleNamespace(text=text, chat_id="10", from_user=SimpleNamespace(id="7"), message_thread_id=None)


def test_normalize_period_accepts_year_month_and_date():
    assert normalize_period("2025") == "2025"
    assert normalize_period("202512") == "2025-12"
    assert normalize_period("2025-12") == "2025-12"
    assert normalize_period("31/12/2025") == "2025-12"


def test_normalize_period_rejects_invalid_values():
    with pytest.raises(ValueError):
        normalize_period("2025-13")
    with pytest.raises(ValueError):
        normalize_period("texto")


def test_visible_cuit_masks_middle_digits():
    assert visible_cuit("20123456789") == "20-******-9"


def test_delivery_accepts_the_v5_consultation_path_and_versions():
    assert is_valid_delivery_path(
        "/home/pancho/clientes/vgs-st-srl/30712345678/agip/2026/07/consultas/"
        "30712345678-ddjj-iibb-agip-2026-07.xlsx"
    )
    assert is_valid_delivery_path(
        "/home/pancho/clientes/vgs-st-srl/30712345678/agip/2026/anual/consultas/"
        "30712345678-ddjj-iibb-agip-2026-v02.xlsx"
    )
    assert not is_valid_delivery_path(
        "/home/pancho/clientes/vgs-st-srl/30712345678/agip/2026/2026-07/ddjj-vep/"
        "2026-08-14__ddjj-iibb-periodo-2026-07.xlsx"
    )


def test_expired_state_is_rejected_before_starting_worker():
    async def scenario():
        flow = AgipDdjjFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        flow.states[key] = FlowState(
            user_id="7",
            nonce="a" * 10,
            stage="period",
            contributor_id=1,
            represented_id=2,
            created_at=time.monotonic() - 601,
        )

        assert await flow.text(adapter, _message("2026-08")) is True

        assert key not in flow.states
        assert "venció" in adapter._bot.send_message.await_args.kwargs["text"]
        assert flow.tasks == {}

    asyncio.run(scenario())


def test_cancel_running_query_terminates_its_process_group(monkeypatch):
    async def scenario():
        flow = AgipDdjjFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, represented_id=2)
        state.progress_message = FakeMessage()
        flow.states[key] = state
        process = FakeRunningProcess()
        flow.processes[key] = process
        kills = []

        def killpg(pid, sig):
            kills.append((pid, sig))
            process.returncode = -sig

        monkeypatch.setattr("plugins.platforms.telegram.agip_ddjj_flow.os.killpg", killpg)
        query = _query("ad:cancel:" + "a" * 10)

        assert await flow.callback(adapter, query, query.data, "10", None, "7") is True

        assert kills == [(4242, signal.SIGTERM)]
        assert state.cancelled is True
        assert "cancelada" in state.progress_message.edits[-1][0].lower()

    asyncio.run(scenario())


def test_worker_starts_in_new_session(monkeypatch):
    async def scenario():
        flow = AgipDdjjFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        state = FlowState(user_id="7", nonce="a" * 10, stage="running", contributor_id=1, represented_id=2)
        process = SimpleNamespace(
            pid=4242,
            returncode=0,
            communicate=AsyncMock(return_value=(json.dumps({"ok": False, "error": "bloqueo"}).encode(), b"")),
            wait=AsyncMock(return_value=0),
        )
        create = AsyncMock(return_value=process)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(flow, "_acquire_execution_lock", lambda _key: None)
        monkeypatch.setattr(flow, "_release_execution_lock", lambda _key: None)

        await flow._run_query(adapter, "10", None, key, state, "2026-08")

        assert create.await_args.kwargs["start_new_session"] is True

    asyncio.run(scenario())


def test_period_starts_tracked_task_with_cancel_button_and_cleans_state(monkeypatch):
    async def scenario():
        flow = AgipDdjjFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        state = FlowState(
            user_id="7", nonce="a" * 10, stage="period",
            contributor_id=1, represented_id=2,
        )
        flow.states[key] = state
        started = asyncio.Event()
        finish = asyncio.Event()

        async def controlled_worker(*_args):
            started.set()
            await finish.wait()

        monkeypatch.setattr(flow, "_run_query", controlled_worker)
        cancel_markup = object()
        monkeypatch.setattr(flow, "_cancel_keyboard", lambda _nonce: cancel_markup)

        assert await flow.text(adapter, _message("2026-08")) is True
        await asyncio.wait_for(started.wait(), timeout=1)

        assert key in flow.tasks
        markup = adapter._bot.send_message.await_args.kwargs["reply_markup"]
        assert markup is cancel_markup

        finish.set()
        await flow.tasks[key]
        await asyncio.sleep(0)
        assert key not in flow.tasks
        assert key not in flow.states

    asyncio.run(scenario())


def test_cross_process_lock_rejects_second_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "plugins.platforms.telegram.agip_ddjj_flow._LOCK_ROOT", tmp_path / "locks"
    )
    first = AgipDdjjFlow()
    second = AgipDdjjFlow()

    first._acquire_execution_lock("1:2:2026-08")
    try:
        with pytest.raises(RuntimeError, match="AGIP_EXECUTION_ALREADY_RUNNING"):
            second._acquire_execution_lock("1:2:2026-08")
    finally:
        first._release_execution_lock("1:2:2026-08")

    second._acquire_execution_lock("1:2:2026-08")
    second._release_execution_lock("1:2:2026-08")


def test_timeout_terminates_process_and_releases_locks(monkeypatch):
    async def scenario():
        flow = AgipDdjjFlow()
        adapter = FakeAdapter()
        key = flow._key("10", None, "7")
        state = FlowState(
            user_id="7", nonce="a" * 10, stage="running",
            contributor_id=1, represented_id=2, progress_message=FakeMessage(),
        )
        release = []

        async def hang():
            await asyncio.Event().wait()

        process = SimpleNamespace(
            pid=4242, returncode=None, communicate=hang, wait=AsyncMock(return_value=0)
        )
        terminate = AsyncMock(side_effect=lambda _proc: setattr(process, "returncode", -signal.SIGTERM))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        monkeypatch.setattr(
            "plugins.platforms.telegram.agip_ddjj_flow._RUN_TIMEOUT_SECONDS", 0.01
        )
        monkeypatch.setattr(flow, "_acquire_execution_lock", lambda _key: None)
        monkeypatch.setattr(flow, "_release_execution_lock", lambda lock_key: release.append(lock_key))
        monkeypatch.setattr(flow, "_terminate_process", terminate)

        await flow._run_query(adapter, "10", None, key, state, "2026-08")

        terminate.assert_awaited_once_with(process)
        assert key not in flow.processes
        assert flow.execution_locks == {}
        assert release == ["1:2:2026-08"]
        assert "tiempo máximo" in state.progress_message.edits[-1][0]

    asyncio.run(scenario())


def test_terminate_process_kills_real_child_process_group():
    async def scenario():
        parent_code = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(child.pid,flush=True); time.sleep(60)"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent_code,
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )
        assert proc.stdout is not None
        child_pid = int((await asyncio.wait_for(proc.stdout.readline(), timeout=3)).decode())

        await AgipDdjjFlow._terminate_process(proc)
        assert proc.returncode is not None

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                stat = Path(f"/proc/{child_pid}/stat").read_text(encoding="ascii").split()[2]
            except FileNotFoundError:
                break
            if stat == "Z":
                break
            await asyncio.sleep(0.05)
        else:
            os.kill(child_pid, signal.SIGKILL)
            pytest.fail("el proceso hijo continuó vivo después de cancelar el grupo")

    asyncio.run(scenario())


def test_worker_error_is_not_reflected_verbatim_to_telegram():
    assert AgipDdjjFlow._worker_error_message(
        {"error": "traceback con ruta privada /home/example y secreto"}
    ) == "Consulta AGIP no completada. La evidencia quedó preservada para revisión."
