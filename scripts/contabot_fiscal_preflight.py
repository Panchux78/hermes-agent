#!/usr/bin/env python3
"""Fail-closed deployment gate for ContaBot fiscal Telegram flows.

Levels 0-3 never contact a tax portal and never commit database changes.
Use ``--phase pre`` before restarting and ``--phase post`` immediately after.
The script prints only check names and aggregate counts; it never prints IDs,
credentials, SQL payloads, contributor data, or subprocess stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any


HERMES_REPO = Path(os.environ.get("HERMES_AGENT_REPO", "/home/pancho/.hermes/hermes-agent"))
# Copia canónica: un clon dedicado de ContaBot en `main` que sigue a `origin/main`. Nadie trabaja
# ahí; el gate la avanza por fast-forward antes de comparar. Antes apuntaba a un worktree de la
# #101 que quedó viejo y sucio, y el gate daba rojo por la copia, no por el runtime.
CONTABOT_REPO = Path(os.environ.get(
    "CONTABOT_CANONICAL_REPO", "/home/pancho/.local/state/contabot/canonical/Contabot"
))
CONTABOT_LIVE = Path(os.environ.get("CONTABOT_LIVE_REPO", "/home/pancho/hermes-workspace/Contabot"))
PORTAL_REPO = Path(os.environ.get("CONTABOT_PORTAL_REPO", "/home/pancho/procedimientos/portal-iva"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/pancho/.hermes"))
PROFILE = Path(os.environ.get("CONTABOT_DB_PROFILE", "/home/pancho/.config/contabot/fiscal-104/database.json"))


class Gate:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, level: int, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"level": level, "name": name, "ok": bool(ok), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def emit(self, phase: str) -> None:
        for item in self.checks:
            status = "PASS" if item["ok"] else "FAIL"
            suffix = f" ({item['detail']})" if item["detail"] else ""
            print(f"L{item['level']} {status} {item['name']}{suffix}")
        totals = {str(level): sum(1 for item in self.checks if item["level"] == level and item["ok"])
                  for level in range(4)}
        print(json.dumps({"ok": self.ok, "phase": phase, "passes": totals}, sort_keys=True))


# El gateway corre con el Node propio de Hermes en el PATH; una corrida manual del gate tiene que
# ver el mismo, o las sondas fallan por entorno y no por el runtime.
_NODE_BIN = HERMES_HOME / "node" / "bin"
if _NODE_BIN.is_dir() and str(_NODE_BIN) not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = str(_NODE_BIN) + os.pathsep + os.environ.get("PATH", "")


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    """Fail closed: un ejecutable ausente o un timeout es un check fallido, no una excepción."""
    try:
        return subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False)
    except (FileNotFoundError, NotADirectoryError, PermissionError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(command, 127, b"", b"")


def sync_canonical(repo: Path) -> tuple[bool, str]:
    """Avanza la copia canónica a origin/main por fast-forward. Nunca pisa trabajo local."""
    if not (repo / ".git").exists():
        return False, "canonical_missing"
    if run(["git", "fetch", "--quiet", "origin", "main"], cwd=repo, timeout=60).returncode:
        return False, "canonical_fetch_failed"
    if git_text(repo, "rev-parse", "--abbrev-ref", "HEAD") != "main":
        return False, "canonical_not_on_main"
    if run(["git", "merge", "--ff-only", "--quiet", "origin/main"], cwd=repo).returncode:
        return False, "canonical_diverged"
    return True, "canonical_at_origin_main"


def git_text(repo: Path, *args: str) -> str:
    result = run(["git", *args], cwd=repo)
    if result.returncode:
        raise RuntimeError("git_check_failed")
    return result.stdout.decode("utf-8", errors="replace").strip()


def git_published(repo: Path) -> tuple[bool, str]:
    if git_text(repo, "status", "--porcelain", "--untracked-files=no"):
        return False, "tracked_worktree_dirty"
    upstream = git_text(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    head = git_text(repo, "rev-parse", "HEAD")
    remote = git_text(repo, "rev-parse", upstream)
    return head == remote, "head_matches_upstream" if head == remote else "head_differs_upstream"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def active_telegram_users() -> list[int]:
    """Enumerate effective internal users through the local administrative role.

    The identifiers are consumed in memory and never printed. The gateway role
    remains unable to read ``console.tbl_usuarios`` directly.
    """
    query = """SET ROLE console_admin;
    SELECT u.telegram_id
    FROM console.tbl_usuarios u
    JOIN console.tbl_roles r ON r.id_rol=u.id_rol
    LEFT JOIN console.tbl_estudios e ON e.id_estudio=u.id_estudio
    WHERE u.telegram_id IS NOT NULL AND u.activo AND r.activo
      AND (r.codigo='admin_global' OR e.activo)
    ORDER BY u.telegram_id;"""
    result = run([
        "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
        "-U", os.environ.get("CONTABOT_CONSOLE_ADMIN_DB_USER", "pancho"),
        "-d", os.environ.get("CONTABOT_CONSOLE_DATABASE", "contabot"),
        "-c", query,
    ])
    values = result.stdout.decode("ascii", errors="strict").splitlines() if result.returncode == 0 else []
    if not values or any(not re.fullmatch(r"[1-9][0-9]{0,18}", item) for item in values):
        raise RuntimeError("telegram_users_unavailable")
    actors = [int(item) for item in values]
    if len(actors) != len(set(actors)):
        raise RuntimeError("telegram_users_duplicated")
    return actors


def load_pg_module():
    path = CONTABOT_LIVE / "lib" / "contabot_pg.py"
    spec = importlib.util.spec_from_file_location("contabot_preflight_pg", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("database_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def psql(capability: str, sql: str) -> subprocess.CompletedProcess[bytes]:
    os.environ["CONTABOT_DB_PROFILE"] = str(PROFILE)
    command, environment = load_pg_module().psql_invocation(capability)
    return run([*command, "-qAt", "-v", "VERBOSITY=sqlstate", "-c", sql], env=environment)


def systemd_environment() -> dict[str, str]:
    result = run(["systemctl", "--user", "show", "hermes-gateway.service", "-p", "Environment", "--value"])
    if result.returncode:
        raise RuntimeError("service_environment_unavailable")
    values: dict[str, str] = {}
    for token in shlex.split(result.stdout.decode("utf-8", errors="replace")):
        if "=" in token:
            name, value = token.split("=", 1)
            values[name] = value
    return values


def router_psql(sql: str) -> subprocess.CompletedProcess[bytes]:
    environment = systemd_environment()
    required = ["CONTABOT_ROUTER_CATALOG_HOST", "CONTABOT_ROUTER_CATALOG_PORT",
                "CONTABOT_ROUTER_CATALOG_DATABASE", "CONTABOT_ROUTER_CATALOG_USER",
                "CONTABOT_ROUTER_CATALOG_PGPASSFILE"]
    if any(not environment.get(name) for name in required):
        raise RuntimeError("router_profile_missing")
    pg_env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    pg_env.update(PGPASSFILE=environment["CONTABOT_ROUTER_CATALOG_PGPASSFILE"], PGCONNECT_TIMEOUT="10")
    return run([
        "psql", "-X", "-w", "--host=" + environment["CONTABOT_ROUTER_CATALOG_HOST"],
        "--port=" + environment["CONTABOT_ROUTER_CATALOG_PORT"],
        "--dbname=" + environment["CONTABOT_ROUTER_CATALOG_DATABASE"],
        "--username=" + environment["CONTABOT_ROUTER_CATALOG_USER"], "-qAt", "-c", sql,
    ], env=pg_env)


def level0(gate: Gate, phase: str) -> None:
    try:
        ok, detail = sync_canonical(CONTABOT_REPO)
    except Exception:
        ok, detail = False, "canonical_sync_failed"
    gate.check(0, "contabot_canonical_synced", ok, detail)
    for name, repo in (("hermes_published", HERMES_REPO), ("contabot_published", CONTABOT_REPO)):
        try:
            ok, detail = git_published(repo)
        except Exception:
            ok, detail = False, "git_check_failed"
        gate.check(0, name, ok, detail)
    try:
        portal_clean = not git_text(PORTAL_REPO, "status", "--porcelain", "--untracked-files=no")
    except Exception:
        portal_clean = False
    gate.check(0, "portal_tracked_tree_clean", portal_clean, "local_repo_without_remote")

    runtime_pairs = (
        (CONTABOT_REPO / "lib/contabot_pg.py", CONTABOT_LIVE / "lib/contabot_pg.py"),
        (CONTABOT_REPO / "scripts/agip-ddjj-worker.py", CONTABOT_LIVE / "scripts/agip-ddjj-worker.py"),
    )
    matching = all(left.is_file() and right.is_file() and sha256(left) == sha256(right)
                   for left, right in runtime_pairs)
    gate.check(0, "contabot_runtime_files_match_canonical", matching)

    active = run(["systemctl", "--user", "is-active", "hermes-gateway.service"])
    gate.check(0, "gateway_active", active.returncode == 0 and active.stdout.strip() == b"active")
    if phase == "post":
        stamp = run(["systemctl", "--user", "show", "hermes-gateway.service", "-p", "ExecMainStartTimestampMonotonic", "--value"])
        try:
            start_us = int(stamp.stdout.strip())
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            boot_now_us = int(uptime * 1_000_000)
            age_seconds = (boot_now_us - start_us) / 1_000_000
            newest_mtime = max(path.stat().st_mtime for path in (
                HERMES_REPO / "plugins/platforms/telegram/agip_ddjj_flow.py",
                HERMES_REPO / "plugins/platforms/telegram/fiscal_credentials.py",
                CONTABOT_LIVE / "lib/contabot_pg.py",
            ))
            process_start_epoch = __import__("time").time() - age_seconds
            fresh = process_start_epoch >= newest_mtime
        except Exception:
            fresh = False
        gate.check(0, "gateway_loaded_current_runtime", fresh)


def function_rows(actor: int, entity: str, capability: str) -> list[str]:
    entity_sql = entity.replace("'", "''")
    query = (
        "SELECT id_representacion||'|'||revision_representacion||'|'||cuit "
        "FROM console.fn_buscar_contribuyente_fiscal("
        f"{actor},'{entity_sql}',NULL,NULL);"
    )
    result = psql(capability, query)
    if result.returncode:
        raise RuntimeError("scope_query_failed")
    return [line for line in result.stdout.decode().splitlines() if line]


def level1(gate: Gate) -> None:
    try:
        actors = active_telegram_users()
        unknown = next(candidate for candidate in range(1, len(actors) + 2) if candidate not in actors)
        allowed = all(
            (result := router_psql(f"SELECT console.fn_telegram_usuario_habilitado({actor});")).returncode == 0
            and result.stdout.strip() == b"t"
            for actor in actors
        )
        denied = router_psql(f"SELECT console.fn_telegram_usuario_habilitado({unknown});")
        gate_ok = allowed and denied.returncode == 0 and denied.stdout.strip() == b"f"
    except Exception:
        actors = []
        gate_ok = False
    gate.check(1, "telegram_user_gate", gate_ok, f"actors={len(actors)}")
    database_ok = True
    verified_rollbacks = 0
    try:
        for actor in actors:
            for capability in ("lookup", "fiscal"):
                linked = psql(capability, f"SELECT console.fn_actor_telegram_vinculado({actor});")
                if linked.returncode or linked.stdout.strip() != b"t":
                    raise RuntimeError("actor_not_linked")
                for entity in ("ARCA", "AGIP - Clave Ciudad"):
                    if not function_rows(actor, entity, capability):
                        raise RuntimeError("entity_candidates_missing")
            relation_id, revision, cuit = function_rows(actor, "ARCA", "fiscal")[0].split("|")
            transaction = psql(
                "verification",
                "BEGIN; SELECT console.fn_verificar_representacion_fiscal("
                f"{actor},{int(relation_id)},{int(revision)},'ARCA','{cuit}'); ROLLBACK;",
            )
            if transaction.returncode or b"t" not in transaction.stdout.splitlines():
                raise RuntimeError("verification_rollback_failed")
            verified_rollbacks += 1
    except Exception:
        database_ok = False
    gate.check(1, "fiscal_roles_and_scopes", database_ok,
               f"actors={len(actors)} rollback_checks={verified_rollbacks}")

    try:
        router = router_psql("SELECT 1;")
        router_ok = router.returncode == 0 and router.stdout.strip() == b"1"
    except Exception:
        router_ok = False
    gate.check(1, "router_catalog_connection", router_ok)


def level2(gate: Gate) -> None:
    tests = [
        "tests/gateway/test_fiscal_scope.py",
        "tests/gateway/test_fiscal_permissions_721.py",
        "tests/gateway/test_fiscal_adapter_integration.py",
        "tests/gateway/test_portal_iva_flow.py",
        "tests/gateway/test_agip_ddjj_flow.py",
        "tests/gateway/test_lea_fiscal_query_flow.py",
        "tests/gateway/test_vencimientos_flow.py",
    ]
    result = run([str(HERMES_REPO / "venv/bin/python"), "-m", "pytest", "-q", *tests],
                 cwd=HERMES_REPO, timeout=300)
    match = re.search(rb"(\d+) passed", result.stdout)
    detail = f"tests={match.group(1).decode()}" if match else "tests_failed"
    gate.check(2, "gateway_fiscal_flows_without_portal", result.returncode == 0, detail)


def level3(gate: Gate) -> None:
    programs = ("node", "firefox", "xvfb-run")
    gate.check(3, "required_programs", all(shutil.which(name) for name in programs))
    fiscal_python = Path(os.environ.get(
        "CONTABOT_FISCAL_RUNTIME_PYTHON",
        "/home/pancho/.local/share/contabot/fiscal-runtime/bin/python",
    ))
    dependency_check = run([
        str(fiscal_python), "-I", "-B", "-c",
        "from importlib.metadata import version; "
        "assert version('openpyxl') == '3.1.5'; "
        "assert version('selenium') == '4.48.0'",
    ], timeout=30) if fiscal_python.is_file() else None
    gate.check(3, "fiscal_python_dependencies", bool(
        dependency_check is not None and dependency_check.returncode == 0
    ))
    probes = (
        HERMES_HOME / "skills/productivity/ccma-obligaciones-pagos/scripts/arca_ccma_probe.js",
        HERMES_HOME / "skills/productivity/sct-estado-cumplimiento/scripts/sct_probe.js",
    )
    probe_ok = all(run(["node", str(probe), "--preflight"], timeout=30).stdout.strip()
                   == b"result=fiscal_runtime_ready" for probe in probes if probe.is_file())
    gate.check(3, "ccma_sct_runtime_preflight", probe_ok and all(path.is_file() for path in probes))

    worker_python = Path("/home/pancho/hermes-workspace/agip-consulta-2025/.venv-selenium/bin/python")
    worker = CONTABOT_LIVE / "scripts/agip-ddjj-worker.py"
    result = run([str(worker_python), str(worker), "999999999", "999999998", "2025"], timeout=30)
    try:
        lines = [json.loads(line) for line in result.stdout.decode().splitlines() if line.startswith("{")]
        worker_ok = (result.returncode != 0 and len(lines) == 1 and lines[0].get("ok") is False
                     and lines[0].get("error_code") == "AGIP_ACCESS_UNAVAILABLE")
    except Exception:
        worker_ok = False
    gate.check(3, "agip_worker_strict_error_json", worker_ok)

    portal = PORTAL_REPO / "portal_iva.py"
    portal_ok = portal.is_file() and run([sys.executable, str(portal), "--help"], timeout=30).returncode == 0
    gate.check(3, "portal_iva_executor_loads", portal_ok)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("pre", "post"), default="pre")
    args = parser.parse_args()
    gate = Gate()
    level0(gate, args.phase)
    level1(gate)
    level2(gate)
    level3(gate)
    gate.emit(args.phase)
    return 0 if gate.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
