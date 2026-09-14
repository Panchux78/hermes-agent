"""Canonical access and human interaction. Only synthetic data/processes."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import fiscal_credentials as credentials
from plugins.platforms.telegram import fiscal_interaction as interaction
from plugins.platforms.telegram.fiscal_execution import terminate_owned_group
from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow, _WorkflowMenuState


def test_login_submission_diagnostics_exclude_values(tmp_path):
    from plugins.platforms.telegram.ccma_diagnostics import Diagnostics
    recorder = Diagnostics(tmp_path)
    try:
        for code in ('captcha_answer_received', 'captcha_input_verified', 'login_fields_verified',
                     'captcha_input_not_retained', 'login_fields_not_retained', 'captcha_rejected'):
            recorder.record({'stage': 'login', 'code': code, 'solution': 'PRIVATE-ANSWER',
                             'password': 'PRIVATE-PASSWORD'})
    finally:
        recorder.close()
    content = (tmp_path / 'diagnostic.jsonl').read_text()
    assert 'unknown_runner_status' not in content
    assert 'PRIVATE' not in content


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['good', 'empty', 'ambiguous', 'wrong_holder', 'wrong_subject', 'empty_password'])
async def test_canonical_access_validates_one_bound_row_without_exposing_secret(monkeypatch, variant):
    row = {'usuario': '20123456783', 'representado': '20987654321', 'password': 'synthetic-not-real'}
    if variant == 'wrong_holder': row['usuario'] = '20000000001'
    if variant == 'wrong_subject': row['representado'] = '20000000001'
    if variant == 'empty_password': row['password'] = ''
    raw = json.dumps(row).encode() + b'\n'
    if variant == 'empty': raw = b''
    if variant == 'ambiguous': raw *= 2
    calls = []
    def invocation(capability):
        calls.append(capability)
        # Real pipe in a local child; fake psql only, never a live database.
        return [sys.executable, '-c', f'import sys;sys.stdout.buffer.write({raw!r})'], {}
    monkeypatch.setitem(sys.modules, 'contabot_pg', NS(psql_invocation=invocation))
    if variant == 'good':
        value = await credentials.canonical_access(8, '20987654321', 'synthetic-client', '20123456783')
        assert json.loads(value)['password'] == 'synthetic-not-real'
        assert json.loads(value)['usuario'] != json.loads(value)['representado']
    else:
        with pytest.raises(ValueError, match='canonical_access'):
            await credentials.canonical_access(8, '20987654321', 'synthetic-client', '20123456783')
    assert calls == ['fiscal']


@pytest.mark.asyncio
async def test_missing_profile_has_no_privileged_or_csv_fallback(monkeypatch):
    def unavailable(capability): raise RuntimeError('missing')
    monkeypatch.setitem(sys.modules, 'contabot_pg', NS(psql_invocation=unavailable))
    child = AsyncMock()
    monkeypatch.setattr(credentials.asyncio, 'create_subprocess_exec', child)
    with pytest.raises(credentials.FiscalDatabaseError, match='fiscal_database_unavailable'):
        await credentials.canonical_access(8, '20987654321', 'synthetic-client', '20123456783')
    child.assert_not_awaited()


def make_flow():
    flow = FiscalQueryFlow()
    bot = NS(send_photo=AsyncMock(return_value=NS(message_id=91)))
    flow.bind(NS(_bot=bot, send=AsyncMock(), send_document=AsyncMock(), _background_tasks=set()))
    state = _WorkflowMenuState('ccma_obligaciones_pagos', 'CCMA', stage='running')
    flow._workflow_menu_state[('7', '8')] = state
    return flow, state


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['ccma', 'sct'])
@pytest.mark.parametrize('direct', [False, True])
async def test_streaming_captcha_immediate_reply_identity_and_current_message(tmp_path, operation, direct):
    flow, state = make_flow()
    state.skill_command = 'sct_estado_cumplimiento' if operation == 'sct' else 'ccma_obligaciones_pagos'
    nonce = 'a' * 16
    image = tmp_path / f'captcha-{nonce}.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic'); image.chmod(0o600)
    marker = 'FISCAL_CAPTCHA:' + json.dumps({'nonce': nonce, 'path': str(image)})
    code = (f'import sys,json; print({marker!r},file=sys.stderr,flush=True); '
            f'a=json.loads(sys.stdin.readline()); assert a=={{"nonce":{nonce!r},"solution":"ABCD"}}; '
            'print("result=source_copied",flush=True)')
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    task = asyncio.create_task(interaction.communicate(flow, proc, ('7', '8'), '7', tmp_path))
    try:
        for _ in range(100):
            if state.captcha_message_id == 91: break
            await asyncio.sleep(.01)
        assert not task.done()  # image delivered while child is waiting, not after exit
        assert state.captcha_message_id == 91
        flow._adapter._bot.send_photo.assert_awaited_once()
        def message(uid, reply):
            return NS(text='ABCD', chat=NS(id=7, type='private'), from_user=NS(id=uid), reply_to_message=NS(message_id=reply) if reply is not None else None)
        assert not await flow.text(flow._adapter, message(9,None if direct else 91))
        await flow.text(flow._adapter, message(8,90))
        assert not state.captcha_response.done()
        await flow.text(flow._adapter, message(8,None if direct else 91))
        assert await asyncio.wait_for(task, 3) == b'result=source_copied\n'
        assert state.stage == 'running' and state.captcha_response is None
        assert any(call.args[1] == 'Respuesta recibida. Verificando el ingreso a ARCA…'
                   for call in flow.send.await_args_list)
    finally:
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        await terminate_owned_group(proc)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['timeout', 'cancel'])
async def test_captcha_pending_cleanup(tmp_path, monkeypatch, action):
    flow, state = make_flow()
    nonce = 'b'*16
    image = tmp_path / f'captcha-{nonce}.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic'); image.chmod(0o600)
    code = 'import sys,time;print(' + repr('FISCAL_CAPTCHA:'+json.dumps({'nonce':nonce,'path':str(image)})) + ',file=sys.stderr,flush=True);time.sleep(30)'
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    monkeypatch.setattr(interaction, 'CAPTCHA_TIMEOUT', .2)
    task = asyncio.create_task(interaction.communicate(flow, proc, ('7','8'), '7', tmp_path))
    try:
        for _ in range(100):
            if state.captcha_message_id == 91: break
            await asyncio.sleep(.01)
        if action == 'cancel': task.cancel()
        with pytest.raises((asyncio.CancelledError, asyncio.TimeoutError)):
            await task
        assert state.captcha_response is None
    finally:
        await terminate_owned_group(proc)
        assert proc.returncode is not None


@pytest.mark.parametrize('kind', ['outside','symlink','public'])
def test_captcha_image_containment(tmp_path, kind):
    root=tmp_path/'private'; root.mkdir(mode=0o700)
    path=root/('captcha-'+'c'*16+'.png'); path.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic'); path.chmod(0o600)
    if kind=='outside': root=root/'other'; root.mkdir()
    if kind=='symlink': original=path.with_suffix('.original');path.rename(original);path.symlink_to(original)
    if kind=='public': path.chmod(0o660)
    with pytest.raises(ValueError):
        interaction.captcha_bytes(json.dumps({'nonce':'c'*16,'path':str(path)}),root)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['ccma','sct'])
async def test_dispatch_uses_private_stdin_and_never_a_csv(tmp_path,monkeypatch,operation):
    from plugins.platforms.telegram import ccma_dispatch, fiscal_query_flow
    monkeypatch.setattr('plugins.platforms.telegram.fiscal_runtime.require_fiscal_database', AsyncMock())
    home=tmp_path/'profile'; scripts=home/'skills/productivity'/('ccma-obligaciones-pagos' if operation=='ccma' else 'sct-estado-cumplimiento')/'scripts'
    scripts.mkdir(parents=True)
    marker=tmp_path/'pipe-checked'
    probe=scripts/('arca_ccma_probe.js' if operation=='ccma' else 'sct_probe.js')
    probe.write_text("if(process.argv[2]==='--preflight'){console.log('result=fiscal_runtime_ready');process.exit(0);}"
        "const fs=require('fs'),rl=require('readline').createInterface({input:process.stdin});"
        "if(process.env.ARCA_CSV_FILE||process.env.ARCA_CSV_LINE||process.env.PASSWORD)process.exit(8);"
        "rl.once('line',line=>{const a=JSON.parse(line);if(a.password!=='synthetic-only'||a.usuario===a.representado)process.exit(9);"
        "fs.writeFileSync("+json.dumps(str(marker))+",'checked');console.log('result=subject_not_verified');rl.close();process.stdin.destroy();});")
    (scripts/'sct_xlsx.py').touch()
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setenv('HOME',str(tmp_path));monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('CONTABOT_CLIENTES_ROOT',str(tmp_path/'clients'))
    monkeypatch.setattr(fiscal_query_flow,'_WORKFLOW_MENU_OUTPUT_DIR',str(tmp_path/'output'))
    payload=json.dumps({'type':'access','usuario':'20123456783','representado':'20987654321','password':'synthetic-only'}).encode()+b'\n'
    accessor=AsyncMock(return_value=payload)
    monkeypatch.setattr(ccma_dispatch if operation=='ccma' else fiscal_query_flow,'canonical_access',accessor)
    flow=FiscalQueryFlow(catalog=NS(runtime_python=sys.executable));flow.send=AsyncMock();flow.send_document=AsyncMock()
    args=dict(chat_id='7',state_key=('7','8'),contributor_id=8,holder_cuit='20123456783',
        client_slug='client',client_cuit='20987654321',credential_line=None,credential_sha256=None)
    if operation=='ccma': await ccma_dispatch.run_ccma(flow,**args,period_from='01/2026',period_to='01/2026')
    else: await flow._run_sct_dispatch(**args,period_mode='range',period_from='20260100',period_until='20260131',period_label='01/2026')
    accessor.assert_awaited_once_with(8,'20987654321','client','20123456783')
    assert marker.read_text()=='checked'
    assert not list(tmp_path.rglob('*.csv'))
    assert 'synthetic-only' not in str(flow.send.await_args_list)
    flow.send_document.assert_not_awaited()
