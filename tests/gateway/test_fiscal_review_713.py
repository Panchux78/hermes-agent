"""Synthetic regressions: real workbook and owned parent/child, no portals."""
import asyncio
import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from openpyxl import load_workbook
from plugins.platforms.telegram import ccma_workbook, ccma_dispatch
from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow
import plugins.platforms.telegram.fiscal_query_flow as fiscal_module


@pytest.mark.parametrize('amount', ['22,307.45', 'NO_LEGIBLE'])
def test_ccma_literal_text_and_unreadable_amount(tmp_path, monkeypatch, amount):
    source, output = tmp_path/'source.csv', tmp_path/'output.xlsx'
    with source.open('w', newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(ccma_workbook.SOURCE_HEADERS)
        writer.writerow(['','Detalle','01/2026','999','999','0','=1+1','01/01/2026',amount,'0.00',amount])
    source.chmod(0o600)
    for key,value in {'CCMA_SOURCE_FILE':str(source),'CCMA_WORKBOOK_FILE':str(output),
            'CCMA_PERIOD_LABEL':'01/2026','CCMA_SOURCE_SHA256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'CCMA_GENERATED_AT':'synthetic'}.items():
        monkeypatch.setenv(key,value)
    if amount=='NO_LEGIBLE':
        with pytest.raises(ValueError, match='ccma_amount_unreadable'):
            ccma_workbook.main()
        assert not output.exists()
    else:
        ccma_workbook.main()
        book=load_workbook(output)
        assert book['Fuente CCMA']['G2'].value=='=1+1'
        assert book['Fuente CCMA']['G2'].data_type=='s'
        assert book['Trabajo CCMA']['G2'].data_type=='s'
        book.close()


def test_sct_runner_uses_explicit_private_source(tmp_path, monkeypatch):
    home=tmp_path/'profile'
    access=tmp_path/'private'/'source.csv'
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('ARCA_CSV_FILE',str(access))
    env=FiscalQueryFlow()._sct_runner_env(credential_line=2,credential_sha256='0'*64,
        period_mode='empty',period_from='',period_until='',source_csv=tmp_path/'source.csv',
        login_screenshot=tmp_path/'login.png',service_screenshot=tmp_path/'service.png',result_screenshot=tmp_path/'result.png')
    assert env['HERMES_HOME']==str(home)
    assert env['ARCA_CSV_FILE']==str(access)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['valid', 'changed', 'collision'])
async def test_sct_freezes_selected_source_before_real_subprocess(tmp_path, monkeypatch, state):
    monkeypatch.setattr(fiscal_module, 'require_fiscal_runtime', AsyncMock())
    home = tmp_path / 'profile'
    scripts = home / 'skills/productivity/sct-estado-cumplimiento/scripts'
    scripts.mkdir(parents=True)
    source = tmp_path / 'private/source.csv'
    frozen = source.with_suffix('.access.csv')
    access = tmp_path / 'configured-access.csv'
    access.write_bytes(b'synthetic-selected-version')
    access.chmod(0o600)
    expected = hashlib.sha256(access.read_bytes()).hexdigest()
    observed = tmp_path / 'observed.json'
    (scripts / 'sct_probe.js').write_text(
        'import os,pathlib,hashlib,json\n'
        f'pathlib.Path({str(access)!r}).write_bytes(b"synthetic-later-version")\n'
        'p=pathlib.Path(os.environ["ARCA_CSV_FILE"])\n'
        f'pathlib.Path({str(observed)!r}).write_text(json.dumps({{'
        '"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),'
        '"path":str(p),"profile":os.environ["HERMES_HOME"],'
        '"mode":p.stat().st_mode & 0o777}))\n'
        'print("result=sct_handoff_login_not_verified")\n'
    )
    (scripts / 'sct_xlsx.py').touch()
    (home / 'bin').mkdir()
    (home / 'bin/uv').touch()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('ARCA_CSV_FILE', str(access))
    monkeypatch.setattr(fiscal_module.shutil, 'which', lambda _: sys.executable)
    flow = FiscalQueryFlow()
    flow.send = AsyncMock()
    flow.send_document = AsyncMock()
    monkeypatch.setattr(flow, '_sct_dispatch_paths', lambda: (
        tmp_path/'result.xlsx', source, tmp_path/'login.png',
        tmp_path/'service.png', tmp_path/'result.png'))
    if state == 'changed':
        access.write_bytes(b'synthetic-changed-before-start')
    if state == 'collision':
        frozen.parent.mkdir()
        frozen.write_bytes(b'preexisting-owned-by-another-run')
    await flow._run_sct_dispatch(chat_id='synthetic', state_key=('0', '0'),
        credential_line=2, credential_sha256=expected, period_mode='empty',
        period_from='', period_until='', period_label='',
        client_slug='synthetic', client_cuit='00000000000')
    if state == 'valid':
        assert json.loads(observed.read_text()) == {
            'sha256':expected, 'path':str(frozen), 'profile':str(home), 'mode':0o600}
        assert access.read_bytes() == b'synthetic-later-version'
    else:
        assert not observed.exists()
    if state == 'collision':
        assert frozen.read_bytes() == b'preexisting-owned-by-another-run'
    else:
        assert not frozen.exists()
    assert not flow._sct_dispatch_processes
    flow.send_document.assert_not_awaited()


@pytest.mark.linux_only
@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['cancel', 'timeout'])
async def test_ccma_stops_its_real_child(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(ccma_dispatch, 'require_fiscal_runtime', AsyncMock())
    libc=ctypes.CDLL(None,use_errno=True)
    previous=ctypes.c_int()
    assert libc.prctl(37,ctypes.byref(previous),0,0,0)==0
    assert libc.prctl(36,1,0,0,0)==0
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    home=tmp_path/'profile'; monkeypatch.setenv('HERMES_HOME',str(home))
    scripts=home/'skills/productivity/ccma-obligaciones-pagos/scripts';scripts.mkdir(parents=True)
    access=tmp_path/'access.csv';access.write_text('synthetic-only');access.chmod(0o600)
    monkeypatch.setenv('ARCA_CSV_FILE',str(access))
    child_file=tmp_path/'child.pid'
    child_ready=tmp_path/'child.ready'
    child_code=('import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        f'pathlib.Path({str(child_ready)!r}).touch(); time.sleep(30)')
    (scripts/'arca_ccma_probe.js').write_text('import subprocess,sys,time,pathlib\n'
        f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
        f'while not pathlib.Path({str(child_ready)!r}).exists(): time.sleep(.01)\n'
        f'pathlib.Path({str(child_file)!r}).write_text(str(p.pid))\n'
        'time.sleep(30)\n')
    monkeypatch.setattr(ccma_dispatch.shutil,'which',lambda name:sys.executable)
    real_wait_for=asyncio.wait_for
    async def short_timeout(aw,timeout):
        return await real_wait_for(aw,timeout=2 if timeout==240 else timeout)
    if operation=='timeout': monkeypatch.setattr(asyncio,'wait_for',short_timeout)
    flow=NS(catalog=NS(runtime_python=sys.executable),send=AsyncMock(),send_document=AsyncMock(),
        _sct_dispatch_processes={},_sct_runner_status=FiscalQueryFlow._sct_runner_status)
    task=asyncio.create_task(ccma_dispatch.run_ccma(flow,chat_id='synthetic',state_key=('0','0'),
        credential_line=2,credential_sha256=hashlib.sha256(access.read_bytes()).hexdigest(),
        period_from='01/2026',period_to='01/2026',client_slug='synthetic',client_cuit='00000000000'))
    child=None
    try:
        for _ in range(150):
            if child_file.exists(): break
            if task.done(): raise AssertionError('synthetic runner did not start')
            await asyncio.sleep(.02)
        assert child_file.exists()
        child=int(child_file.read_text())
        parent = flow._sct_dispatch_processes[('0','0')].pid
        if operation=='cancel': task.cancel()
        await real_wait_for(asyncio.gather(task,return_exceptions=True),8)
        dead=False
        for _ in range(100):
            found,_=os.waitpid(child,os.WNOHANG)
            if found==child: dead=True;break
            await asyncio.sleep(.02)
        assert dead,'child survived parent cancellation/timeout'
        child=None
        with pytest.raises(ProcessLookupError):
            os.kill(parent,0)
        assert not flow._sct_dispatch_processes
        assert access.read_text() == 'synthetic-only'
        for run in (tmp_path/'hermes-workspace/output/private/ccma-runs').iterdir():
            assert not (run/'access.csv').exists()
    finally:
        if not task.done(): task.cancel(); await asyncio.gather(task,return_exceptions=True)
        if child is not None:
            try: os.kill(child,signal.SIGKILL)
            except ProcessLookupError: pass
            os.waitpid(child,0)
        assert libc.prctl(36,previous.value,0,0,0)==0
