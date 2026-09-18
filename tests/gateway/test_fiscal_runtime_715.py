"""Offline runtime, publication and error regressions; no Telegram or portal."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pwd
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.fiscal_runtime import require_fiscal_runtime
from plugins.platforms.telegram.ccma_artifact import sct_destination, publish_named
from plugins.platforms.telegram.ccma_dispatch import run_ccma
from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow


@pytest.mark.asyncio
async def test_preflight_checks_selected_python_without_installing(tmp_path, monkeypatch):
    probe = tmp_path/'probe.js'
    probe.write_text("if(process.argv[2]!=='--preflight')process.exit(9);console.log('result=fiscal_runtime_ready')")
    node = shutil.which('node')
    assert node, 'offline Node runtime required for this suite'
    await require_fiscal_runtime(sys.executable, node, probe, tmp_path, canonical=False)
    import json
    probe.write_text("if(process.env.FISCAL_RUNTIME_PYTHON!=="+
                     json.dumps(sys.executable)+")process.exit(9);console.log('result=fiscal_runtime_ready')")
    await require_fiscal_runtime(sys.executable, node, probe, tmp_path, canonical=False)
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(tmp_path/'browser-cache'))
    probe.write_text("if(process.env.PLAYWRIGHT_BROWSERS_PATH!=="+
        json.dumps(str(tmp_path/'browser-cache'))+")process.exit(9);console.log('result=fiscal_runtime_ready')")
    await require_fiscal_runtime(sys.executable, node, probe, tmp_path, canonical=False)
    flow = FiscalQueryFlow(catalog=NS(runtime_python=sys.executable))
    environment = flow._sct_runner_env(credential_line=2,credential_sha256='0'*64,period_mode='empty',
        period_from='',period_until='',source_csv=tmp_path/'source.csv',
        login_screenshot=tmp_path/'login.png',service_screenshot=tmp_path/'service.png',
        result_screenshot=tmp_path/'result.png')
    assert environment['PLAYWRIGHT_BROWSERS_PATH']==str(tmp_path/'browser-cache')
    assert environment['FISCAL_RUNTIME_PYTHON']==sys.executable
    empty = tmp_path/'empty-venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(empty)], check=True, capture_output=True)
    with pytest.raises(RuntimeError, match='fiscal_python_missing'):
        await require_fiscal_runtime(empty/'bin/python', node, probe, tmp_path, canonical=False)
    probe.write_text("process.exit(1)")
    with pytest.raises(RuntimeError, match='fiscal_browser_missing'):
        await require_fiscal_runtime(sys.executable, node, probe, tmp_path, canonical=False)
    assert not (empty/'bin/pip').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['ccma', 'sct'])
async def test_missing_dependency_stops_before_reading_access_or_opening_portal(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    path = {'ccma': 'ccma-obligaciones-pagos/scripts/arca_ccma_probe.js',
            'sct': 'sct-estado-cumplimiento/scripts/sct_probe.js'}[kind]
    probe = tmp_path/'skills/productivity'/path
    probe.parent.mkdir(parents=True)
    marker = tmp_path/'portal-was-started'
    probe.write_text(f"require('fs').writeFileSync({str(marker)!r},'bad');")
    probe.with_name('sct_xlsx.py').touch()
    flow = FiscalQueryFlow(catalog=NS(runtime_python=tmp_path/'missing/python'))
    flow.send = AsyncMock()
    flow.send_document = AsyncMock()
    common = dict(chat_id='1', state_key=('1','1'), credential_line=2, credential_sha256='0'*64,
                  period_from='01/2026', client_slug='synthetic', client_cuit='00000000000')
    if kind == 'ccma':
        await run_ccma(flow, **common, period_to='01/2026')
    else:
        await flow._run_sct_dispatch(**common, period_mode='empty', period_until='', period_label='')
    assert not marker.exists()
    assert 'No se consultó ARCA' in flow.send.await_args.args[1]
    flow.send_document.assert_not_awaited()


@pytest.mark.parametrize('mode,start,end,reference,folder', [
    ('range','20260000','20261231','2026','2026/anual'),
    ('range','20260600','20260631','2026-06','2026/06'),
    ('range','20260100','20260631','202601-202606','2026/anual'),
    ('empty','','','sin-filtro',''),
])
def test_sct_naming_uses_validated_identity_and_scope(tmp_path, mode, start, end, reference, folder):
    directory, name = sct_destination(tmp_path, 'cliente-prueba', '00000000000', mode, start, end)
    assert name == f'cliente-prueba-sct-estado-cumplimiento-arca-{reference}.xlsx'
    assert directory == tmp_path/'cliente-prueba/00000000000/arca'/folder/'consultas'
    slug='cliente-ccma-obligaciones-pagos-arca-prueba'
    _, unusual=sct_destination(tmp_path,slug,'00000000000',mode,start,end)
    assert unusual.startswith(slug+'-sct-estado-cumplimiento-arca-')
    with pytest.raises(ValueError):
        sct_destination(tmp_path, '../other', '00000000000', mode, start, end)
    with pytest.raises(ValueError):
        sct_destination(tmp_path, 'cliente', '00000000000', 'range', '20260600', '20260131')


def test_publication_concurrent_no_partial_no_clobber(tmp_path, monkeypatch):
    source = tmp_path/'source.xlsx'
    source.write_bytes(b'synthetic-complete-book')
    directory = tmp_path/'legajo'
    with ThreadPoolExecutor(max_workers=4) as executor:
        files = list(executor.map(lambda _: publish_named(source, directory, 'book.xlsx'), range(4)))
    assert len(set(files)) == 4
    assert {p.name for p in files} == {'book.xlsx','book-v02.xlsx','book-v03.xlsx','book-v04.xlsx'}
    assert directory.stat().st_mode & 0o777 == 0o750
    assert all(p.read_bytes() == source.read_bytes() and p.stat().st_mode & 0o777 == 0o640 for p in files)
    def interrupted(inp, out):
        out.write(b'partial')
        raise OSError('simulated_copy_failure')
    monkeypatch.setattr('plugins.platforms.telegram.ccma_artifact.shutil.copyfileobj', interrupted)
    with pytest.raises(OSError):
        publish_named(source, directory, 'book.xlsx')
    assert set(directory.iterdir()) == set(files)
    target = tmp_path/'target'; target.mkdir()
    link = tmp_path/'link'; link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        publish_named(source, link/'must-not-exist', 'book.xlsx')
    assert not list(target.iterdir())


def test_publication_preserves_read_only_named_acl(tmp_path):
    if not shutil.which('setfacl') or not shutil.which('getfacl'):
        pytest.skip('POSIX ACL tools unavailable')
    try:
        service_uid = pwd.getpwnam('contabot-console').pw_uid
    except KeyError:
        pytest.skip('console service identity unavailable')
    root = tmp_path / 'clients'
    root.mkdir()
    subprocess.run(
        ['setfacl', '-m', f'u:{service_uid}:r-x,d:u:{service_uid}:r-x', str(root)],
        check=True,
    )
    source = tmp_path / 'source.xlsx'
    source.write_bytes(b'synthetic-complete-book')

    target = publish_named(source, root / 'client/year/consultas', 'book.xlsx')

    directory_acl = subprocess.run(
        ['getfacl', '-cpn', str(target.parent)], check=True, text=True,
        stdout=subprocess.PIPE,
    ).stdout
    file_acl = subprocess.run(
        ['getfacl', '-cpn', str(target)], check=True, text=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert f'user:{service_uid}:r-x' in directory_acl
    assert f'user:{service_uid}:r-x\t#effective:r--' in file_acl


def test_publication_versions_ics_without_changing_extension(tmp_path):
    source = tmp_path / 'calendar.ics'
    source.write_bytes(b'BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n')
    directory = tmp_path / 'client/arca/consultas'

    first = publish_named(source, directory, 'client-vencimientos-arca.ics')
    second = publish_named(source, directory, 'client-vencimientos-arca.ics')

    assert first.name == 'client-vencimientos-arca.ics'
    assert second.name == 'client-vencimientos-arca-v02.ics'
    assert first.read_bytes() == second.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_ccma_reports_unreadable_amount_without_private_diagnostics(tmp_path, monkeypatch):
    verify = AsyncMock()
    monkeypatch.setattr('plugins.platforms.telegram.ccma_dispatch.verify_representation', verify)
    monkeypatch.setenv('HOME',str(tmp_path)); monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    scripts=tmp_path/'skills/productivity/ccma-obligaciones-pagos/scripts'
    scripts.mkdir(parents=True)
    access=tmp_path/'.arca.csv';access.write_bytes(b'synthetic');access.chmod(0o600)
    import csv, io, json
    from plugins.platforms.telegram.ccma_workbook import SOURCE_HEADERS
    content=io.StringIO();writer=csv.writer(content);writer.writerow(SOURCE_HEADERS)
    writer.writerow(['','Detalle','01/2026','999','999','0','synthetic','01/01/2026','unreadable','0.00','0.00'])
    (scripts/'arca_ccma_probe.js').write_text(
        "if(process.argv[2]==='--preflight'){console.log('result=fiscal_runtime_ready');process.exit(0);}"
        "const fs=require('fs'),crypto=require('crypto'),source="+json.dumps(content.getvalue())+";"
        "fs.writeFileSync(process.env.ARCA_EXPORT_FILE,source,{mode:0o600});"
        "console.log('result=source_copied\\nsource_sha256='+crypto.createHash('sha256').update(source).digest('hex'));")
    flow=FiscalQueryFlow(catalog=NS(runtime_python=sys.executable))
    flow.send=AsyncMock();flow.send_document=AsyncMock()
    await run_ccma(flow,chat_id='1',state_key=('1','1'),credential_line=2,
        credential_sha256=hashlib.sha256(access.read_bytes()).hexdigest(),
        period_from='01/2026',period_to='01/2026',client_slug='synthetic',client_cuit='00000000000',
        telegram_id=1,scope_item={'id_contribuyente': 1})
    assert 'importe ilegible; no se generó el libro' in flow.send.await_args.args[1]
    flow.send_document.assert_not_awaited()
    assert not list(tmp_path.rglob('*.xlsx'))
