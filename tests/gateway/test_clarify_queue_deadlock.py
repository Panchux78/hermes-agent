"""Regression: queue mode must not deadlock behind clarify."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    source = _source()
    key = build_session_key(source)
    entry = SessionEntry(
        session_key=key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    store = MagicMock()
    store.get_or_create_session.return_value = entry
    store.load_transcript.return_value = []
    store.has_any_sessions.return_value = True
    store.append_to_transcript = MagicMock()
    store.rewrite_transcript = MagicMock()
    store.update_session = MagicMock()
    runner.session_store = store

    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)

    import time
    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "waiting for user clarify response",
        "api_call_count": 12,
        "max_iterations": 150,
    }
    runner._running_agents[key] = agent
    runner._running_agents_ts[key] = time.time() - 120
    return runner, adapter, key


def _clear():
    from tools import clarify_gateway
    with clarify_gateway._lock:
        clarify_gateway._entries.clear()
        clarify_gateway._session_index.clear()
        clarify_gateway._notify_cbs.clear()


@pytest.mark.asyncio
async def test_queue_mode_cancels_unmatched_clarify_before_queueing_followup():
    _clear()
    from tools import clarify_gateway

    runner, adapter, key = _runner()
    pending = clarify_gateway.register(
        "clarify-1", key, "Pick one", ["A", "B"]
    )

    await runner._handle_message(
        MessageEvent(text="Why are you not responding?", source=_source(), message_id="m2")
    )

    assert pending.event.is_set(), "the waiting clarify turn must be released"
    queued = adapter._pending_messages.get(key) or runner._pending_messages.get(key)
    assert queued is not None
    assert queued.text == "Why are you not responding?"
    _clear()
