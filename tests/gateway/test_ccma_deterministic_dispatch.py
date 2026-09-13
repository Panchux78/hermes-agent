import asyncio
import csv
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from plugins.platforms.telegram.ccma_dispatch import run_ccma
from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow

@pytest.mark.asyncio
async def test_repeated_scope_executes_twice_and_delivers_only_new_workbooks(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    home=tmp_path/'.hermes';monkeypatch.setenv('HERMES_HOME',str(home))
    scripts=home/'skills/productivity/ccma-obligaciones-pagos/scripts';scripts.mkdir(parents=True)
    credentials=home/'.arca.csv';credentials.write_text('synthetic credentials');credentials.chmod(0o600)
    headers=['Detalle','Periodo','Impuesto','Concepto','Subpcto','Descripción','Fecha Movimiento','Debe','Haber','Saldo']
    import io
    content=io.StringIO();writer=csv.writer(content);writer.writerow(headers)
    writer.writerow(['','Detalle','01/2025','20','19','19','Movimiento','01/01/2025','10,00','0,00','10,00'])
    source=content.getvalue()
    probe=scripts/'arca_ccma_probe.js'
    probe.write_text("const fs=require('fs'),crypto=require('crypto');const text="+json.dumps(source)+";fs.writeFileSync(process.env.ARCA_EXPORT_FILE,text,{mode:0o600});console.log('result=source_copied\\nsource_sha256='+crypto.createHash('sha256').update(text).digest('hex'));" )
    flow=NS(catalog=NS(runtime_python=sys.executable),send=AsyncMock(),send_document=AsyncMock(return_value=NS(success=True)),
            _sct_dispatch_processes={},_sct_runner_status=FiscalQueryFlow._sct_runner_status, handle_message=AsyncMock())
    kwargs=dict(chat_id='7',state_key=('7','7'),credential_line=2,
                credential_sha256=hashlib.sha256(credentials.read_bytes()).hexdigest(),period_from='01/2025',period_to='12/2025')
    await run_ccma(flow,**kwargs);await run_ccma(flow,**kwargs)
    assert flow.send_document.await_count==2
    files=[Path(c.kwargs['file_path']) for c in flow.send_document.await_args_list]
    assert files[0] != files[1]
    for file in files:
        assert file.is_file() and file.stat().st_mode & 0o777 == 0o600
        assert not (file.parent/'access.csv').exists()
        assert (file.parent/'fuente.csv').read_text() == source.replace('\r\n','\n')
    flow.handle_message.assert_not_awaited()
    probe.write_text("console.log('result=runner_error')")
    await run_ccma(flow,**kwargs)
    assert flow.send_document.await_count==2
    assert 'No se generó ni se reenvió' in flow.send.await_args.args[1]
