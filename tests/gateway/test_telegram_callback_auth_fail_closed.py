"""Tests for Telegram adapter fail-closed auth fallback (#24457).

The _is_callback_user_authorized fallback must deny users by default
when TELEGRAM_ALLOWED_USERS is empty, instead of allowing everyone.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig, Platform


# -- Fake telegram modules (minimal stubs) --------------------------------

_fake_telegram_error = types.ModuleType("telegram.error")


class _TelegramError(Exception):
    pass


_fake_telegram_error.TelegramError = _TelegramError
_fake_telegram_error.BadRequest = type("BadRequest", (_TelegramError,), {})
_fake_telegram_error.NetworkError = type("NetworkError", (_TelegramError,), {})

_fake_telegram_constants = types.ModuleType("telegram.constants")
_fake_telegram_constants.ParseMode = SimpleNamespace(HTML="HTML")

_fake_telegram_request = types.ModuleType("telegram.request")
_fake_telegram_request.HTTPXRequest = type("HTTPXRequest", (), {"__init__": lambda *a, **kw: None})

_fake_telegram_ext = types.ModuleType("telegram.ext")
_fake_telegram_ext.ApplicationBuilder = type("ApplicationBuilder", (), {
    "token": lambda self, *a: self,
    "build": lambda self: None,
})

_fake_telegram = types.ModuleType("telegram")
_fake_telegram.error = _fake_telegram_error
_fake_telegram.constants = _fake_telegram_constants
_fake_telegram.ext = _fake_telegram_ext
_fake_telegram.request = _fake_telegram_request


@pytest.fixture(autouse=True)
def _inject_fake_telegram(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram", _fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", _fake_telegram_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", _fake_telegram_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", _fake_telegram_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", _fake_telegram_request)


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = object.__new__(TelegramAdapter)
    adapter.config = config
    adapter._config = config
    adapter._platform = Platform.TELEGRAM
    adapter._connected = True
    return adapter


class TestCallbackAuthFailClosed:
    """_is_callback_user_authorized fallback must be fail-closed."""

    def test_no_allowlist_no_allow_all_denies(self, monkeypatch):
        """No TELEGRAM_ALLOWED_USERS and no GATEWAY_ALLOW_ALL_USERS → deny."""
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        # Force the fallback path (no runner auth)
        adapter._message_handler = None
        assert adapter._is_callback_user_authorized("12345") is False


    def test_allowlist_with_matching_user_permits(self, monkeypatch):
        """TELEGRAM_ALLOWED_USERS contains the user → allow."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345,67890")
        adapter = _make_adapter()
        adapter._message_handler = None
        assert adapter._is_callback_user_authorized("12345") is True


class TestGatewayRestartButton:
    def test_restart_button_schedules_a_delayed_user_service_restart(self, monkeypatch):
        """The callback must schedule a restart after Telegram can acknowledge it."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        launched = []
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.subprocess.Popen",
            lambda args, **kwargs: launched.append((args, kwargs)),
        )

        adapter = _make_adapter()
        adapter._schedule_gateway_restart()

        assert len(launched) == 1
        args, kwargs = launched[0]
        assert args[:4] == ["systemd-run", "--user", "--on-active=2s", "--collect"]
        assert args[-4:] == ["systemctl", "--user", "restart", "hermes-gateway.service"]
        assert kwargs["start_new_session"] is True

    def test_restart_notice_mentions_the_imminent_gateway_restart(self):
        """The user-visible notice must be sent before the restart is scheduled."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        assert TelegramAdapter._gateway_restart_notice() == "⚠️ El gateway se reiniciará en unos segundos…"

    def test_rich_messages_include_the_persistent_menu_trigger(self):
        """Rich replies must install the one-button persistent menu."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        assert TelegramAdapter._persistent_menu_reply_markup() == {
            "keyboard": [[{"text": "☰ Menú"}]],
            "resize_keyboard": True,
            "is_persistent": True,
        }


