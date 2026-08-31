"""Tests for lazy forum command registration in TelegramAdapter."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


def _make_test_adapter():
    """Build a TelegramAdapter without running __init__."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    # ``name`` is a property derived from platform.value.title()
    adapter._bot = MagicMock()
    adapter._bot.set_my_commands = AsyncMock()
    adapter._bot.delete_my_commands = AsyncMock()
    adapter._forum_command_registered = set()
    adapter._forum_lock = asyncio.Lock()
    return adapter


def _forum_message(chat_id=-100, is_forum=True):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, is_forum=is_forum),
    )


@pytest.mark.asyncio
async def test_ensure_forum_commands_clears_legacy_scope_once():
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-123, is_forum=True)

    with patch("telegram.BotCommandScopeChat") as MockScope:
        # Track the chat_id passed to the BotCommandScopeChat constructor
        # so the assertions below see an int instead of a bare MagicMock.
        def _make_scope(chat_id):
            s = MagicMock()
            s.chat_id = chat_id
            return s
        MockScope.side_effect = _make_scope
        await adapter._ensure_forum_commands(msg)

    assert -123 in adapter._forum_command_registered
    adapter._bot.set_my_commands.assert_not_awaited()
    adapter._bot.delete_my_commands.assert_awaited_once()
    _, kwargs = adapter._bot.delete_my_commands.call_args
    assert kwargs["scope"] is not None
    assert isinstance(kwargs["scope"].chat_id, int)
    assert kwargs["scope"].chat_id == -123


@pytest.mark.asyncio
async def test_ensure_forum_commands_race_safety():
    """Two concurrent coroutines must not double-register the same chat."""
    adapter = _make_test_adapter()
    msg = _forum_message(chat_id=-789, is_forum=True)

    with patch("telegram.BotCommandScopeChat"):
        coro1 = adapter._ensure_forum_commands(msg)
        coro2 = adapter._ensure_forum_commands(msg)
        await asyncio.gather(coro1, coro2)

    # The lock should make this exactly 1 call, not 2.
    assert adapter._bot.delete_my_commands.await_count == 1
