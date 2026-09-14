"""Immediate, nonce-correlated human CAPTCHA transport; no LLM or stored answer."""
import asyncio
import json
import os
from pathlib import Path
import re
import stat

CAPTCHA_TIMEOUT = 300
RUN_TIMEOUT = 240 + 3 * CAPTCHA_TIMEOUT


def captcha_bytes(raw, root):
    request = json.loads(raw)
    if not isinstance(request, dict) or set(request) != {'nonce', 'path'}:
        raise ValueError('captcha_request_invalid')
    nonce = request['nonce']
    if not isinstance(nonce, str) or not re.fullmatch('[a-f0-9]{16}', nonce):
        raise ValueError('captcha_request_invalid')
    path = Path(request['path'])
    root = Path(root)
    if (not root.is_absolute() or root.resolve() != root or path.parent != root
            or path.name != f'captcha-{nonce}.png' or path.is_symlink()):
        raise ValueError('captcha_path_invalid')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as image:
        info = os.fstat(image.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 1000000):
            raise ValueError('captcha_path_invalid')
        data = image.read(1000001)
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 1000000:
        raise ValueError('captcha_image_invalid')
    return nonce, data


async def communicate(flow, process, key, chat_id, root, initial=None, on_diagnostic=None):
    """Keep stdout results separate; process each challenge as soon as emitted."""
    if initial is not None:
        process.stdin.write(initial)
        await process.stdin.drain()
    # Legacy test runners may not implement streams. Real child always does.
    if not getattr(process, 'stdout', None) or not getattr(process, 'stderr', None):
        return (await process.communicate())[0]
    seen = set()

    async def challenges():
        while line := await process.stderr.readline():
            if on_diagnostic is not None and line.startswith(b'FISCAL_DIAGNOSTIC:'):
                try:
                    event = json.loads(line[len(b'FISCAL_DIAGNOSTIC:'):])
                except (ValueError, UnicodeError):
                    event = {'code': 'diagnostic_invalid'}
                on_diagnostic(event)
                continue
            if not line.startswith(b'FISCAL_CAPTCHA:'):
                continue  # never echo browser diagnostics, credentials or page HTML
            nonce, data = captcha_bytes(line[len(b'FISCAL_CAPTCHA:'):].strip(), root)
            if nonce in seen or len(seen) >= 3:
                raise ValueError('captcha_request_repeated')
            seen.add(nonce)
            state = flow._workflow_menu_state.get(key)
            if state is None or state.captcha_response is not None:
                raise ValueError('captcha_state_invalid')
            state.stage = 'captcha'
            state.captcha_nonce = nonce
            state.captcha_response = asyncio.get_running_loop().create_future()
            try:
                # First network operation: image. No agent turn or progress queue.
                sent = await flow._adapter._bot.send_photo(
                    chat_id=chat_id, photo=data,
                    caption='ARCA solicita un CAPTCHA. Respondé a ESTA imagen con sus caracteres. Si vence, te enviaré la nueva.',
                    reply_markup=flow._cancel_keyboard(state))
                state.captcha_message_id = sent.message_id
                solution = await asyncio.wait_for(state.captcha_response, CAPTCHA_TIMEOUT)
                process.stdin.write(json.dumps({'nonce': nonce, 'solution': solution}).encode() + b'\n')
                await process.stdin.drain()
            finally:
                state.stage = 'running'
                state.captcha_response = state.captcha_nonce = state.captcha_message_id = None

    async def output():
        data = bytearray()
        while chunk := await process.stdout.read(65536):
            data.extend(chunk)
            if len(data) > 262144:
                raise ValueError('fiscal_output_too_large')
        return bytes(data)

    tasks = [asyncio.create_task(output()), asyncio.create_task(challenges()), asyncio.create_task(process.wait())]
    group = asyncio.gather(*tasks)
    try:
        results = await asyncio.wait_for(group, RUN_TIMEOUT)
        return results[0]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(group, return_exceptions=True)
