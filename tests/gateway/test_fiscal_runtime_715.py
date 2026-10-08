"""Offline runtime, publication and error regressions; no Telegram or portal."""
import asyncio
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.fiscal_runtime import require_fiscal_runtime
from datetime import date

from plugins.platforms.telegram.ccma_artifact import ccma_destino, sct_destino, vencimientos_destino
from tests.gateway.documentos_falsos import instalar as instalar_documentos
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
    with pytest.raises(RuntimeError, match='fiscal_browser_start_failed'):
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
                  period_from='01/2026', client_slug='synthetic', client_cuit='00000000000',
                  scope_item={'id': 1})
    if kind == 'ccma':
        await run_ccma(flow, **common, period_to='01/2026')
    else:
        await flow._run_sct_dispatch(**common, period_mode='empty', period_until='', period_label='')
    assert not marker.exists()
    assert 'No se consultó ARCA' in flow.send.await_args.args[1]
    flow.send_document.assert_not_awaited()


@pytest.mark.parametrize('mode,start,end,reference,anio,mes,desde,hasta', [
    ('range','20260000','20261231','2026',2026,None,'2026-01-01','2026-12-01'),
    ('range','20260600','20260630','2026-06',2026,6,'2026-06-01','2026-06-01'),
    ('range','20260100','20260630','2026-01-a-2026-06',2026,6,'2026-01-01','2026-06-01'),
    ('range','20260100','20261231','2026',2026,None,'2026-01-01','2026-12-01'),
])
def test_sct_destino_uses_validated_identity_and_contract_period(mode, start, end, reference, anio, mes, desde, hasta):
    destino = sct_destino('cliente-prueba', '00000000000', 7, mode, start, end, hoy=date(2026, 9, 5))
    assert destino == {
        'seccion': 'arca', 'anio': anio, 'mes': mes,
        'base': f'cliente-prueba-estado-cumplimiento-arca-{reference}', 'ext': 'xlsx',
        'etiqueta': destino['etiqueta'], 'tipo': 'Estado de cumplimiento', 'origen': 'ARCA',
        'productor': 'sct', 'id_contribuyente': 7, 'periodo_desde': desde, 'periodo_hasta': hasta,
    }
    with pytest.raises(ValueError):
        sct_destino('../other', '00000000000', 7, mode, start, end, hoy=date(2026, 9, 5))
    with pytest.raises(ValueError):
        sct_destino('cliente', '00000000000', 7, 'range', '20260600', '20260131', hoy=date(2026, 9, 5))
    with pytest.raises(ValueError):
        sct_destino('cliente', '00000000000', None, mode, start, end, hoy=date(2026, 9, 5))


def test_sct_without_filter_is_as_of_query_date():
    destino = sct_destino('cliente-prueba', '00000000000', 7, 'empty', '', '', hoy=date(2026, 9, 5))
    assert destino['base'] == 'cliente-prueba-estado-cumplimiento-arca-2026-09-05'
    assert (destino['anio'], destino['mes']) == (2026, 9)
    assert destino['fecha_documento'] == '2026-09-05'
    assert 'periodo_desde' not in destino and 'periodo_hasta' not in destino


@pytest.mark.parametrize('desde,hasta,ref,anio,mes,pdesde,phasta', [
    ('03/2026', '03/2026', '2026-03', 2026, 3, '2026-03-01', '2026-03-01'),
    ('01/2026', '12/2026', '2026', 2026, None, '2026-01-01', '2026-12-01'),
    ('02/2026', '05/2026', '2026-02-a-2026-05', 2026, 5, '2026-02-01', '2026-05-01'),
    ('11/2025', '02/2026', '2025-11-a-2026-02', 2026, 2, '2025-11-01', '2026-02-01'),
])
def test_ccma_destino_follows_contract(desde, hasta, ref, anio, mes, pdesde, phasta):
    destino = ccma_destino('cliente-prueba', '00000000000', 4, desde, hasta)
    assert destino['base'] == f'cliente-prueba-cuenta-corriente-arca-{ref}'
    assert (destino['anio'], destino['mes'], destino['seccion']) == (anio, mes, 'arca')
    assert (destino['periodo_desde'], destino['periodo_hasta']) == (pdesde, phasta)
    assert (destino['tipo'], destino['origen'], destino['productor']) == ('Cuenta corriente', 'ARCA', 'ccma')
    with pytest.raises(ValueError):
        ccma_destino('cliente-prueba', '00000000000', 4, hasta, '01/2020')


def test_vencimientos_destino_year_or_range():
    anual = vencimientos_destino('c', '00000000000', 1, ['2026-03-01', '2026-11-30'], 'xlsx')
    assert (anual['base'], anual['anio'], anual['mes']) == ('c-vencimientos-arca-2026', 2026, None)
    assert (anual['periodo_desde'], anual['periodo_hasta']) == ('2026-01-01', '2026-12-01')
    rango = vencimientos_destino('c', '00000000000', 1, ['2027-01-02', '2026-12-31'], 'ics')
    assert (rango['base'], rango['anio'], rango['mes'], rango['ext']) == (
        'c-vencimientos-arca-2026-12-a-2027-01', 2027, 1, 'ics')
    with pytest.raises(ValueError):
        vencimientos_destino('c', '00000000000', 1, [], 'xlsx')


@pytest.mark.asyncio
async def test_ccma_reports_unreadable_amount_without_private_diagnostics(tmp_path, monkeypatch):
    verify = AsyncMock()
    monkeypatch.setattr('plugins.platforms.telegram.ccma_dispatch.verify_representation', verify)
    monkeypatch.setenv('HOME',str(tmp_path)); monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    documentos = instalar_documentos(monkeypatch, tmp_path)
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
    assert documentos.destinos() == []
