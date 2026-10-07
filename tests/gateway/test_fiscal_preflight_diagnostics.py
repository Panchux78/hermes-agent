"""Real local subprocesses: bounded preflight, cancellation and sanitized errors."""
import asyncio
import os
from pathlib import Path
import sys

import pytest

from plugins.platforms.telegram import fiscal_runtime as runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('script,code', [
    ("raise RuntimeError('fiscal_browser_missing')", 'fiscal_browser_missing'),
    ("raise PermissionError('DO_NOT_LOG_PRIVATE_DATA')", 'fiscal_browser_start_failed'),
    ("print('wrong result')", 'fiscal_runtime_protocol'),
    ("print('x'*40000);print('ready')", 'fiscal_runtime_protocol'),
    ("import sys;sys.stderr.write('x'*40000);print('ready')", 'fiscal_runtime_protocol'),
    ("import time;time.sleep(60)", 'fiscal_browser_timeout'),
])
async def test_preflight_distinguishes_failure_without_logging_output(script, code, caplog):
    with pytest.raises(RuntimeError, match='^' + code + '$'):
        await runtime._check([sys.executable, '-c', script], dict(os.environ), b'ready',
                             timeout=2, component='browser')
    assert code in caplog.text and 'reference=' in caplog.text
    assert 'DO_NOT_LOG_PRIVATE_DATA' not in caplog.text
    assert 'wrong result' not in caplog.text and 'xxxx' not in caplog.text
    if code == 'fiscal_browser_timeout':
        assert 'no están disponibles' not in runtime.unavailable_message('SCT', code)


@pytest.mark.asyncio
async def test_delayed_ready_accepted_and_cancelled_process_group_reaped(tmp_path):
    # The caller's budget must be honored, not a hidden timeout inside _check.
    assert await runtime._check([sys.executable, '-c',
        "import time;time.sleep(2.1);print('ready')"], dict(os.environ), b'ready',
        timeout=8, component='browser')
    marker = tmp_path / 'child.pid'
    script = (
        "import subprocess,sys,time,pathlib;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
    )
    task = asyncio.create_task(runtime._check([sys.executable, '-c', script, str(marker)],
        dict(os.environ), b'ready', timeout=8, component='browser'))
    try:
        async with asyncio.timeout(8):
            while not marker.exists():
                await asyncio.sleep(.05)
        pid = int(marker.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # A zombie is already dead; Linux may leave it for init to reap.
        proc = Path(f'/proc/{pid}/stat')
        assert not proc.exists() or proc.read_text().split(') ', 1)[1].startswith('Z ')
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
