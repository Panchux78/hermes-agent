"""Non-secret deployment paths shared by the existing ContaBot workflows.

Configuration lives in platforms.telegram.extra.contabot, not process-global
environment variables. Omitting it retains the established deployment.
"""
from collections.abc import Mapping
import os
from pathlib import Path

from hermes_constants import get_hermes_home


def deployment_paths(extra: Mapping | None) -> dict[str, Path]:
    settings = (extra or {}).get("contabot", {})
    allowed = {"project_dir", "runtime_python", "portal_iva_executor", "arca_map"}
    if not isinstance(settings, Mapping) or set(settings) - allowed:
        raise ValueError("CONTABOT_DEPLOYMENT_INVALID")
    result = {}
    for name, value in settings.items():
        if (not isinstance(value, str) or not value or any(ord(c) < 32 for c in value)
                or not Path(value).is_absolute() or ".." in Path(value).parts):
            raise ValueError("CONTABOT_DEPLOYMENT_INVALID")
        result[name] = Path(value)
    return result


def checked_python(path: Path) -> str:
    path = Path(path)
    # A venv's python is normally a symlink: do not reject that valid layout.
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("CONTABOT_PYTHON_UNAVAILABLE")
    return str(path)


def pdf_python_command(runtime_python: Path | None, unavailable: str) -> list[str]:
    if runtime_python is not None:
        return [checked_python(runtime_python), "-B"]
    uv = get_hermes_home() / "bin/uv"
    if not uv.is_file():
        raise RuntimeError(unavailable)
    return [str(uv), "run", "--with", "pdfplumber", "--with", "openpyxl", "python3"]
