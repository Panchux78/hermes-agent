import asyncio
import hashlib
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.admin_maintenance_flow import AdminMaintenanceFlow, CandidateState


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state(tmp_path: Path, kind="bcra") -> CandidateState:
    candidate = tmp_path / "candidate.json"
    candidate.write_text('{"bancos":[]}', encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    return CandidateState(kind, "nonce", candidate, report, _hash(candidate), "current-hash", datetime.now(timezone.utc))


def test_bcra_modified_candidate_does_not_apply(monkeypatch, tmp_path):
    async def scenario():
        state = _state(tmp_path)
        state.candidate.write_text('{"bancos":["changed"]}', encoding="utf-8")
        flow = AdminMaintenanceFlow()
        flow._run = AsyncMock()
        monkeypatch.setattr(flow, "_catalog_args", lambda: ("--host", "h"))
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_HOST", "h")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_PORT", "1")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_DATABASE", "d")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_USER", "u")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_PGPASSFILE", "/tmp/p")
        await flow._apply_bcra(SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock())), 1, None, state)
        flow._run.assert_not_awaited()
    asyncio.run(scenario())


def test_bcra_changed_table_does_not_apply(monkeypatch, tmp_path):
    async def scenario():
        state = _state(tmp_path)
        flow = AdminMaintenanceFlow()
        flow._run = AsyncMock()
        flow._snapshot_bcra = AsyncMock(return_value="changed-table-hash")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_HOST", "h")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_PORT", "1")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_DATABASE", "d")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_USER", "u")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_PGPASSFILE", "/tmp/p")
        await flow._apply_bcra(SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock())), 1, None, state)
        flow._run.assert_not_awaited()
    asyncio.run(scenario())


def test_bcra_matching_hashes_apply_once(monkeypatch, tmp_path):
    async def scenario():
        state = _state(tmp_path)
        flow = AdminMaintenanceFlow()
        flow._run = AsyncMock()
        flow._snapshot_bcra = AsyncMock(return_value=state.current_sha256)
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_HOST", "h")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_PORT", "1")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_DATABASE", "d")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_USER", "u")
        monkeypatch.setenv("CONTABOT_BCRA_WRITE_PGPASSFILE", "/tmp/p")
        await flow._apply_bcra(SimpleNamespace(_bot=SimpleNamespace(send_message=AsyncMock())), 1, None, state)
        flow._run.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["arca", "bcra"])
def test_prepare_failure_cleans_run_directory(monkeypatch, tmp_path, kind):
    async def scenario():
        run_dir = tmp_path / kind
        flow = AdminMaintenanceFlow()
        monkeypatch.setattr(flow, "_private_run_dir", lambda: (run_dir.mkdir(), run_dir)[1])
        flow._run = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            if kind == "arca":
                await flow._prepare_arca(SimpleNamespace(), 1, None, "1")
            else:
                await flow._prepare_bcra(SimpleNamespace(), 1, None, "1")
        assert not run_dir.exists()
    asyncio.run(scenario())


def test_pending_candidate_rejects_second_start(monkeypatch, tmp_path):
    async def scenario():
        flow = AdminMaintenanceFlow()
        state = _state(tmp_path, "arca")
        flow._candidates[flow._key(1, None, "1")] = state
        flow._prepare_arca = AsyncMock()
        query = SimpleNamespace(answer=AsyncMock())
        await flow.callback(SimpleNamespace(), query, "oa:arca:start", 1, None, "1")
        flow._prepare_arca.assert_not_awaited()
    asyncio.run(scenario())


def test_arca_candidate_progress_edits_the_original_message(monkeypatch, tmp_path):
    async def scenario():
        current = tmp_path / "mapa-vigente.json"
        current.write_text('{"impuestos":[]}', encoding="utf-8")
        monkeypatch.setattr(
            "plugins.platforms.telegram.admin_maintenance_flow._ARCA_CURRENT", current
        )
        flow = AdminMaintenanceFlow()
        run_dir = tmp_path / "run"
        monkeypatch.setattr(flow, "_private_run_dir", lambda: (run_dir.mkdir(), run_dir)[1])

        async def fake_run(*command, **_kwargs):
            if command[1].endswith("reconstruir_mapa_impuestos_arca.py"):
                Path(command[2]).write_text('{"impuestos":["new"]}', encoding="utf-8")
                return {}
            Path(command[4]).write_text("{}", encoding="utf-8")
            return {"diffs": {"iva": {"added": 1, "removed": 0, "modified": 0}}}

        flow._run = fake_run
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        await flow._prepare_arca(SimpleNamespace(_bot=bot), 7, None, "42", progress_message_id=91)

        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["chat_id"] == 7
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 91
        assert bot.edit_message_text.await_args.kwargs["text"].startswith("Mapa de impuestos:")
    asyncio.run(scenario())


def test_cancelled_runner_terminates_the_entire_simulated_process_group(monkeypatch):
    async def scenario():
        child = SimpleNamespace(pid=4243, terminated=False)
        process = SimpleNamespace(pid=4242, returncode=None, child=child)
        blocked = asyncio.Event()

        async def communicate():
            await blocked.wait()

        async def wait():
            process.returncode = -signal.SIGTERM

        process.communicate = communicate
        process.wait = wait
        process.stdout = SimpleNamespace(read=communicate)
        async def readline(): return b''
        process.stderr = SimpleNamespace(readline=readline)
        create_calls = []
        kill_calls = []

        async def fake_create(*command, **kwargs):
            create_calls.append((command, kwargs))
            return process

        def fake_killpg(pgid, sig):
            kill_calls.append((pgid, sig))
            if pgid == process.pid and sig == signal.SIGTERM:
                process.terminated = True
                child.terminated = True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
        monkeypatch.setattr("plugins.platforms.telegram.admin_maintenance_flow.os.killpg", fake_killpg)
        task = asyncio.create_task(AdminMaintenanceFlow._run("fake-command"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert create_calls[0][1]["start_new_session"] is True
        assert kill_calls == [(process.pid, signal.SIGTERM)]
        assert process.terminated is True
        assert child.terminated is True
    asyncio.run(scenario())


def test_cancel_releases_lock_and_edits_its_original_progress_message():
    async def scenario():
        flow = AdminMaintenanceFlow()
        started = asyncio.Event()
        never = asyncio.Event()

        async def pending_prepare(*_args):
            started.set()
            await never.wait()

        flow._prepare_arca = pending_prepare
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=91)),
            edit_message_text=AsyncMock(),
        )
        adapter = SimpleNamespace(_bot=bot)
        start_query = SimpleNamespace(answer=AsyncMock())
        await flow.callback(adapter, start_query, "oa:arca:start", 7, None, "42")
        await started.wait()
        cancel_query = SimpleNamespace(answer=AsyncMock())
        await flow.callback(adapter, cancel_query, "oa:cancelrun", 7, None, "42")

        assert flow._key(7, None, "42") not in flow._tasks
        bot.send_message.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 91
    asyncio.run(scenario())


def test_cancelled_runner_terminates_real_curl_child_process(tmp_path):
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(30)
            self.send_response(200)
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pid_file = tmp_path / "curl.pid"
    script = ("import pathlib,subprocess,time;" f"p=subprocess.Popen(['curl','--max-time','60','http://127.0.0.1:{server.server_port}/']);" f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));time.sleep(60)")
    async def scenario():
        task = asyncio.create_task(AdminMaintenanceFlow._run(sys.executable, "-c", script))
        for _ in range(100):
            if pid_file.exists(): break
            await asyncio.sleep(0.02)
        assert pid_file.exists()
        curl_pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        for _ in range(100):
            try: os.kill(curl_pid, 0)
            except ProcessLookupError: return
            await asyncio.sleep(0.02)
        pytest.fail("curl child survived process-group cancellation")
    try: asyncio.run(scenario())
    finally: server.shutdown()


def test_bcra_progress_edits_same_message_id():
    async def scenario():
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        await AdminMaintenanceFlow._progress(SimpleNamespace(_bot=bot), 7, 91, "BCRA: 20 de 60 entidades consultadas.")
        bot.send_message.assert_not_awaited()
        bot.edit_message_text.assert_awaited_once_with(chat_id=7, message_id=91, text="BCRA: 20 de 60 entidades consultadas.", reply_markup=None)
    asyncio.run(scenario())
