from pathlib import Path
from unittest.mock import Mock

from plugins.platforms.telegram import contabot_access


def _configure(monkeypatch, pgpass: Path):
    values = {
        "CONTABOT_ROUTER_CATALOG_HOST": "localhost",
        "CONTABOT_ROUTER_CATALOG_PORT": "5432",
        "CONTABOT_ROUTER_CATALOG_DATABASE": "contabot",
        "CONTABOT_ROUTER_CATALOG_USER": "contabot_router_runtime",
        "CONTABOT_ROUTER_CATALOG_PGPASSFILE": str(pgpass),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_not_configured_is_unknown(monkeypatch):
    for name in contabot_access._ENV.values():
        monkeypatch.delenv(name, raising=False)
    assert contabot_access.telegram_user_enabled("123") is None


def test_invalid_identifier_fails_closed(monkeypatch, tmp_path):
    pgpass = tmp_path / "pgpass"
    pgpass.write_text("unused", encoding="utf-8")
    pgpass.chmod(0o600)
    _configure(monkeypatch, pgpass)
    assert contabot_access.telegram_user_enabled("1 OR TRUE") is False


def test_live_boolean_result(monkeypatch, tmp_path):
    pgpass = tmp_path / "pgpass"
    pgpass.write_text("unused", encoding="utf-8")
    pgpass.chmod(0o600)
    _configure(monkeypatch, pgpass)
    run = Mock(return_value=Mock(returncode=0, stdout="t\n"))
    monkeypatch.setattr(contabot_access.subprocess, "run", run)
    assert contabot_access.telegram_user_enabled("123") is True
    command = run.call_args.args[0]
    assert command[-1] == "SELECT console.fn_telegram_usuario_habilitado(123);"
    assert "unused" not in " ".join(command)


def test_database_error_fails_closed(monkeypatch, tmp_path):
    pgpass = tmp_path / "pgpass"
    pgpass.write_text("unused", encoding="utf-8")
    pgpass.chmod(0o600)
    _configure(monkeypatch, pgpass)
    monkeypatch.setattr(contabot_access.subprocess, "run", Mock(return_value=Mock(returncode=2, stdout="")))
    assert contabot_access.telegram_user_enabled("123") is False
