import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).parents[2] / "scripts" / "contabot_fiscal_preflight.py"
SPEC = importlib.util.spec_from_file_location("contabot_fiscal_preflight_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def completed(stdout=b"", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=b"", returncode=returncode)


def test_active_telegram_users_reads_local_admin_without_env_allowlist(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return completed(b"101\n202\n")

    monkeypatch.setattr(preflight, "run", fake_run)
    assert preflight.active_telegram_users() == [101, 202]
    assert calls[0][:4] == ["psql", "-X", "-qAt", "-v"]
    assert "SET ROLE console_admin" in calls[0][-1]


def test_level1_checks_database_gate_and_continues_other_controls(monkeypatch):
    monkeypatch.setattr(preflight, "active_telegram_users", lambda: [101, 202])

    def fake_router(sql):
        if sql == "SELECT 1;":
            return completed(b"1\n")
        return completed(b"f\n" if "(1)" in sql else b"t\n")

    monkeypatch.setattr(preflight, "router_psql", fake_router)
    monkeypatch.setattr(preflight, "function_rows", lambda actor, entity, capability: ["1|1|20999999999"])
    monkeypatch.setattr(preflight, "psql", lambda capability, sql: completed(b"t\n"))
    gate = preflight.Gate()
    preflight.level1(gate)
    assert gate.checks == [
        {"level": 1, "name": "telegram_user_gate", "ok": True, "detail": "actors=2"},
        {"level": 1, "name": "fiscal_roles_and_scopes", "ok": True, "detail": "actors=2 rollback_checks=2"},
        {"level": 1, "name": "router_catalog_connection", "ok": True, "detail": ""},
    ]
