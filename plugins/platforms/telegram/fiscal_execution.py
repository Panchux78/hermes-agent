"""Execution guards shared by the existing CCMA and SCT dispatchers."""
import asyncio
import hashlib
import os
from pathlib import Path
import signal
import stat


def freeze_credentials(source: Path, target: Path, expected_sha256: str) -> None:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError('credential_permissions')
        data = handle.read()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('credential_changed')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


async def terminate_owned_group(process) -> None:
    """For children started with start_new_session=True, including descendants."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2)
    except asyncio.TimeoutError:
        pass
    finally:
        # The leader can exit before a descendant that ignores TERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()
