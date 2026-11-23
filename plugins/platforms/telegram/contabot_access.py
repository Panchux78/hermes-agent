"""Fail-closed Telegram authorization backed by ContaBot console users."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


_ENV = {
    "host": "CONTABOT_ROUTER_CATALOG_HOST",
    "port": "CONTABOT_ROUTER_CATALOG_PORT",
    "database": "CONTABOT_ROUTER_CATALOG_DATABASE",
    "user": "CONTABOT_ROUTER_CATALOG_USER",
    "pgpass": "CONTABOT_ROUTER_CATALOG_PGPASSFILE",
}


def configured() -> bool:
    return all((os.getenv(name) or "").strip() for name in _ENV.values())


def catalog_connection():
    """Existing limited catalog role, also used for the authorized job functions."""
    if not configured():
        raise RuntimeError('contabot_catalog_not_configured')
    pgpass = Path(os.environ[_ENV['pgpass']])
    info = pgpass.lstat()
    if (not pgpass.is_absolute() or pgpass.resolve()!=pgpass or not pgpass.is_file() or
            info.st_uid!=os.geteuid() or info.st_mode&0o077):
        raise RuntimeError('contabot_catalog_permissions_invalid')
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    env.update(PGPASSFILE=str(pgpass),PGCONNECT_TIMEOUT='5',PGAPPNAME='contabot-telegram-catalog')
    command=['psql','-X','-w','-h',os.environ[_ENV['host']],'-p',os.environ[_ENV['port']],
             '-U',os.environ[_ENV['user']],'-d',os.environ[_ENV['database']]]
    return command,env


def telegram_user_enabled(user_id: str, *, timeout_seconds: float = 2.0) -> bool | None:
    """Return the live table verdict; ``None`` means this deployment is not configured.

    A configured-but-unavailable database fails closed (False). ``user_id`` is
    parsed as an unsigned decimal before it reaches SQL.
    """
    if not configured():
        return None
    candidate = str(user_id or "").strip()
    if not candidate.isdecimal():
        return False
    numeric = int(candidate)
    if numeric <= 0 or numeric > 9223372036854775807:
        return False
    pgpass = Path((os.getenv(_ENV["pgpass"]) or "").strip())
    try:
        stat = pgpass.lstat()
        if pgpass.is_symlink() or not pgpass.is_file() or stat.st_mode & 0o077:
            return False
    except OSError:
        return False
    env = os.environ.copy()
    env["PGPASSFILE"] = str(pgpass)
    command = [
        "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
        "-h", os.environ[_ENV["host"]], "-p", os.environ[_ENV["port"]],
        "-U", os.environ[_ENV["user"]], "-d", os.environ[_ENV["database"]],
        "-c", f"SELECT console.fn_telegram_usuario_habilitado({numeric});",
    ]
    try:
        result = subprocess.run(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=timeout_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    value = result.stdout.strip().lower()
    return value in {"t", "true", "1"}
