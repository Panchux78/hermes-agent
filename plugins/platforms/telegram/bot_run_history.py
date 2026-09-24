"""Cliente común del historial durable de operaciones iniciadas por Telegram."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_DEFAULT_PROJECT = Path("/home/pancho/hermes-workspace/Contabot")
_GATEWAY_INSTANCE_ID = str(uuid.uuid4())
_RECOVERY_LOCK = asyncio.Lock()
_RECOVERED = False


@dataclass(frozen=True)
class RunHandle:
    run_id: int
    item_id: int
    attempt: int = 1
    previous_run_id: int | None = None


class BotRunHistory:
    """Única frontera Hermes → contrato SQL de corridas de ContaBot."""

    def __init__(self, project_dir: Path | None = None, *, instance_id: str | None = None) -> None:
        self.project_dir = Path(
            project_dir
            or os.getenv("CONTA_PDF_ROUTER_PROJECT_DIR", str(_DEFAULT_PROJECT))
        )
        self.instance_id = instance_id or _GATEWAY_INSTANCE_ID

    @property
    def script(self) -> Path:
        path = self.project_dir / "skills/accounting/pdf-contable-router/scripts/bot_runs.py"
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("HISTORY_RUNTIME_UNAVAILABLE")
        return path

    async def _call(self, *arguments: str) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(self.script), *arguments,
            cwd=str(self.project_dir), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("HISTORY_TIMEOUT") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("HISTORY_RESPONSE_INVALID") from exc
        if proc.returncode != 0 or not isinstance(payload, dict) or payload.get("status") != "OK":
            raise RuntimeError("HISTORY_UNAVAILABLE")
        return payload

    async def recover_once(self) -> None:
        global _RECOVERED
        if _RECOVERED:
            return
        async with _RECOVERY_LOCK:
            if _RECOVERED:
                return
            await self._call("recover", "--instance", self.instance_id)
            _RECOVERED = True

    @staticmethod
    def key(*parts: object) -> str:
        return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()

    async def start(
        self, *, telegram_id: int, operation: str, key_material: str,
        reference: str, lease_seconds: int = 3700, attempt: int = 1,
        previous_run_id: int | None = None,
    ) -> RunHandle:
        await self.recover_once()
        args = [
            "start", "--telegram-id", str(telegram_id), "--operation", operation,
            "--idempotency-key", self.key(key_material), "--instance", self.instance_id,
            "--reference", Path(reference).name, "--lease-seconds", str(lease_seconds),
            "--attempt", str(attempt),
        ]
        if previous_run_id is not None:
            args += ["--previous-run-id", str(previous_run_id)]
        payload = await self._call(*args)
        run_id, item_id = payload.get("id_corrida"), payload.get("id_item")
        if not isinstance(run_id, int) or not isinstance(item_id, int):
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        return RunHandle(run_id, item_id, attempt, previous_run_id)

    async def reject(
        self, *, telegram_id: int, operation: str, key_material: str,
        reference: str, reason_code: str, reason_text: str,
        contributor_id: int | None = None,
    ) -> RunHandle:
        """Persiste, de forma idempotente, un rechazo previo al ejecutor."""
        args = [
            "reject", "--telegram-id", str(telegram_id), "--operation", operation,
            "--idempotency-key", self.key("rejection", key_material),
            "--instance", self.instance_id, "--reference", Path(reference).name,
            "--reason-code", reason_code, "--reason-text", reason_text,
        ]
        if contributor_id is not None:
            args += ["--contributor-id", str(contributor_id)]
        payload = await self._call(*args)
        run_id, item_id = payload.get("id_corrida"), payload.get("id_item")
        if not isinstance(run_id, int) or not isinstance(item_id, int):
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        return RunHandle(run_id, item_id)

    async def prepare_items(self, run: RunHandle, references: list[str]) -> list[dict[str, Any]]:
        payload = await self._call(
            "prepare-items", "--run-id", str(run.run_id), "--instance", self.instance_id,
            "--references-json", json.dumps(references, ensure_ascii=False, separators=(",", ":")),
        )
        items = payload.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        return items

    async def start_batch(
        self, *, telegram_id: int, batch_id: str, reference: str,
        lease_seconds: int = 3700,
    ) -> RunHandle:
        await self.recover_once()
        payload = await self._call(
            "start-batch", "--telegram-id", str(telegram_id), "--batch-id", batch_id,
            "--instance", self.instance_id, "--reference", Path(reference).name,
            "--lease-seconds", str(lease_seconds),
        )
        run_id, item_id, attempt = (
            payload.get("id_corrida"), payload.get("id_item"), payload.get("intento")
        )
        previous = payload.get("id_corrida_anterior")
        if not isinstance(run_id, int) or not isinstance(item_id, int) or not isinstance(attempt, int):
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        if previous is not None and not isinstance(previous, int):
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        return RunHandle(run_id, item_id, attempt, previous)

    async def attach(
        self, run: RunHandle, item_id: int, *, contributor_id: int,
        output: Path, output_relative: str,
    ) -> None:
        await self._call(
            "attach", "--run-id", str(run.run_id), "--item-id", str(item_id),
            "--instance", self.instance_id, "--contributor-id", str(contributor_id),
            "--output", str(output), "--output-relative", output_relative,
        )

    async def finish_item(
        self, run: RunHandle, item_id: int, *, state: str, reason_code: str,
        reason_text: str, effects: list[dict[str, Any]], contributor_id: int | None = None,
        period: str | None = None, delivered_count: int | None = None,
        declared_count: int | None = None, output: Path | None = None,
        output_relative: str | None = None,
    ) -> None:
        args = [
            "finish-item", "--run-id", str(run.run_id), "--item-id", str(item_id),
            "--instance", self.instance_id, "--state", state,
            "--reason-code", reason_code, "--reason-text", reason_text,
            "--effects-json", json.dumps(effects, ensure_ascii=False, separators=(",", ":")),
        ]
        if contributor_id is not None:
            args += ["--contributor-id", str(contributor_id)]
        if period:
            args += ["--period", period]
        if delivered_count is not None:
            args += ["--delivered-count", str(delivered_count)]
        if declared_count is not None:
            args += ["--declared-count", str(declared_count)]
        if output is not None and output_relative is not None:
            args += ["--output", str(output), "--output-relative", output_relative]
        await self._call(*args)

    async def close(self, run: RunHandle) -> str:
        payload = await self._call(
            "close", "--run-id", str(run.run_id), "--instance", self.instance_id,
        )
        state = payload.get("estado")
        if state not in {"completada", "fallida", "incompleta", "cancelada", "interrumpida", "rechazada"}:
            raise RuntimeError("HISTORY_RESPONSE_INVALID")
        return str(state)

    async def finish_single(self, run: RunHandle, **kwargs: Any) -> str:
        await self.finish_item(run, run.item_id, **kwargs)
        return await self.close(run)
