"""Ágora #115: Hermes guarda por el módulo único de documentos de ContaBot.

Datos sintéticos; el CLI de documentos es un subprocess real con un script falso.
"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import documentos_contabot as documentos
from plugins.platforms.telegram.ccma_dispatch import run_ccma
from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow
from tests.gateway.documentos_falsos import instalar as instalar_documentos

MENSAJE = "El estudio no tiene espacio suficiente. Liberá archivos desde Documentos o pedí ampliar la cuota."


# ------------------------------------------------------------------ puerta CLI

@pytest.mark.asyncio
async def test_guardar_devuelve_ruta_nueva_y_pasa_el_destino(tmp_path, monkeypatch):
    falso = instalar_documentos(monkeypatch, tmp_path)
    fuente = tmp_path / "x.xlsx"
    fuente.write_bytes(b"libro")
    destino = {"seccion": "arca", "anio": 2026, "mes": 8, "base": "c-cuenta-corriente-arca-2026-08",
               "ext": "xlsx", "etiqueta": "e", "tipo": "Cuenta corriente", "productor": "ccma",
               "origen": "ARCA", "id_contribuyente": 5}
    hecho = await documentos.guardar(fuente, destino)
    assert hecho["ruta"] == "estudios/1/5/2026/08/arca/c-cuenta-corriente-arca-2026-08.xlsx"
    assert (falso.raiz / hecho["ruta"]).read_bytes() == b"libro"
    assert falso.destinos() == [destino]
    assert fuente.exists()


@pytest.mark.asyncio
async def test_cuota_y_errores_nunca_se_informan_como_guardado(tmp_path, monkeypatch):
    fuente = tmp_path / "x.xlsx"
    fuente.write_bytes(b"libro")
    destino = {"seccion": "arca", "anio": 2026, "mes": None, "base": "b", "ext": "xlsx",
               "etiqueta": "e", "tipo": "t", "productor": "p", "id_contribuyente": 5}
    instalar_documentos(monkeypatch, tmp_path, cuota="guardar")
    with pytest.raises(documentos.CuotaInsuficiente) as error:
        await documentos.guardar(fuente, destino)
    assert error.value.mensaje == MENSAJE and error.value.codigo == "cuota_insuficiente"
    instalar_documentos(monkeypatch, tmp_path, cuota="espacio")
    with pytest.raises(documentos.CuotaInsuficiente):
        await documentos.espacio(5, 2_000_000)
    instalar_documentos(monkeypatch, tmp_path, falla="guardar")
    with pytest.raises(documentos.DocumentoNoGuardado) as error:
        await documentos.guardar(fuente, destino)
    assert not isinstance(error.value, documentos.CuotaInsuficiente)
    assert error.value.codigo == "base_no_disponible"


@pytest.mark.asyncio
async def test_respuesta_invalida_o_ruta_fuera_de_estudios_falla(tmp_path, monkeypatch):
    proyecto = tmp_path / "release"
    script = proyecto / "skills/accounting/pdf-contable-router/scripts/documentos_cliente.py"
    script.parent.mkdir(parents=True)
    monkeypatch.setenv("CONTA_PDF_ROUTER_PROJECT_DIR", str(proyecto))
    fuente = tmp_path / "x.xlsx"
    fuente.write_bytes(b"x")
    for salida, codigo in (
        ("print('Traceback')", "documentos_respuesta_invalida"),
        ("import json;print(json.dumps({'status':'OK','id_archivo':1,'ruta':'cliente/2026/x.xlsx'}))",
         "documentos_respuesta_invalida"),
        ("import json,sys;print(json.dumps({'status':'OK','id_archivo':1,'ruta':'estudios/1/x'}));sys.exit(1)",
         "documentos_error"),
    ):
        script.write_text(salida)
        with pytest.raises(documentos.DocumentoNoGuardado) as error:
            await documentos.guardar(fuente, {})
        assert error.value.codigo == codigo
    script.unlink()
    with pytest.raises(documentos.DocumentoNoGuardado, match="documentos_no_disponible"):
        await documentos.espacio(1, 1)


@pytest.mark.parametrize("salida", [
    {"status": "ERROR", "codigo": "cuota_insuficiente", "mensaje": MENSAJE},
    {"ok": False, "error_code": "CUOTA_INSUFICIENTE"},
    {"status": "BLOCKED", "reason": "cuota_insuficiente"},
    {"ok": False, "casos": [{"motivo_codigo": "cuota_insuficiente"}]},
    b'{"status":"ERROR","codigo":"cuota_insuficiente"}\n',
    "ROUTER_CUOTA_INSUFICIENTE",
])
def test_mensaje_si_cuota_es_defensivo(salida):
    assert documentos.mensaje_si_cuota(salida) == MENSAJE


@pytest.mark.parametrize("salida", [None, {}, {"status": "BLOCKED", "reason": "unidentified"}, "ROUTER_TIMEOUT"])
def test_mensaje_si_cuota_no_inventa(salida):
    assert documentos.mensaje_si_cuota(salida) is None


# ------------------------------------------------------------------ CCMA y SCT

def _ccma(tmp_path, monkeypatch):
    monkeypatch.setattr("plugins.platforms.telegram.ccma_dispatch.verify_representation", AsyncMock())
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    scripts = home / "skills/productivity/ccma-obligaciones-pagos/scripts"
    scripts.mkdir(parents=True)
    credentials = home / ".arca.csv"
    credentials.write_text("synthetic credentials")
    credentials.chmod(0o600)
    marker = tmp_path / "arca-consultada"
    headers = ["Detalle", "Periodo", "Impuesto", "Concepto", "Subpcto", "Descripción",
               "Fecha Movimiento", "Debe", "Haber", "Saldo"]
    source = ",".join(headers) + "\n,Detalle,01/2025,20,19,19,Movimiento,01/01/2025,1.00,0.00,1.00\n"
    (scripts / "arca_ccma_probe.js").write_text(
        "if(process.argv.includes('--preflight')){console.log('result=fiscal_runtime_ready');process.exit(0);}"
        f"const fs=require('fs'),crypto=require('crypto');fs.writeFileSync({json.dumps(str(marker))},'1');"
        f"const text={json.dumps(source)};fs.writeFileSync(process.env.ARCA_EXPORT_FILE,text,{{mode:0o600}});"
        "console.log('result=source_copied\\nsource_sha256='+crypto.createHash('sha256').update(text).digest('hex'));")
    flow = NS(catalog=NS(runtime_python=sys.executable), send=AsyncMock(),
              send_document=AsyncMock(return_value=NS(success=True)),
              _sct_dispatch_processes={}, _sct_runner_status=FiscalQueryFlow._sct_runner_status)
    kwargs = dict(chat_id="7", state_key=("7", "7"), credential_line=2,
                  credential_sha256=hashlib.sha256(credentials.read_bytes()).hexdigest(),
                  period_from="11/2025", period_to="02/2026", client_slug="cliente-prueba",
                  client_cuit="20123456783", telegram_id=7, scope_item={"id": 4})
    return flow, kwargs, marker


@pytest.mark.asyncio
async def test_ccma_sin_espacio_no_consulta_arca(tmp_path, monkeypatch):
    falso = instalar_documentos(monkeypatch, tmp_path, cuota="espacio")
    flow, kwargs, marker = _ccma(tmp_path, monkeypatch)
    await run_ccma(flow, **kwargs)
    assert not marker.exists()
    assert flow.send.await_args.args[1] == MENSAJE
    flow.send_document.assert_not_awaited()
    assert falso.llamadas("espacio")[0]["--contribuyente"] == "4"
    assert falso.llamadas("espacio")[0]["--bytes"] == "2000000"
    assert falso.destinos() == []


@pytest.mark.asyncio
async def test_ccma_cuota_al_guardar_informa_mensaje_y_no_entrega(tmp_path, monkeypatch):
    falso = instalar_documentos(monkeypatch, tmp_path, cuota="guardar")
    flow, kwargs, marker = _ccma(tmp_path, monkeypatch)
    await run_ccma(flow, **kwargs)
    assert marker.exists()
    assert flow.send.await_args.args[1] == MENSAJE
    flow.send_document.assert_not_awaited()
    destino = falso.destinos()[0]
    assert destino["base"] == "cliente-prueba-cuenta-corriente-arca-2025-11-a-2026-02"
    assert (destino["anio"], destino["mes"]) == (2026, 2)
    assert (destino["periodo_desde"], destino["periodo_hasta"]) == ("2025-11-01", "2026-02-01")
    assert not (tmp_path / "clientes").exists()


@pytest.mark.asyncio
async def test_ccma_falla_del_modulo_no_informa_exito(tmp_path, monkeypatch):
    instalar_documentos(monkeypatch, tmp_path, falla="guardar")
    flow, kwargs, _ = _ccma(tmp_path, monkeypatch)
    await run_ccma(flow, **kwargs)
    text = flow.send.await_args.args[1]
    assert "no se pudo guardar en Documentos" in text and "finalizada" not in text
    flow.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_ccma_rango_entrega_con_nombre_de_documentos(tmp_path, monkeypatch):
    falso = instalar_documentos(monkeypatch, tmp_path)
    flow, kwargs, _ = _ccma(tmp_path, monkeypatch)
    await run_ccma(flow, **kwargs)
    assert flow.send_document.await_args.kwargs["file_name"] == \
        "cliente-prueba-cuenta-corriente-arca-2025-11-a-2026-02.xlsx"
    assert [p.relative_to(falso.raiz).as_posix() for p in falso.guardados()] == [
        "estudios/1/4/2026/02/arca/cliente-prueba-cuenta-corriente-arca-2025-11-a-2026-02.xlsx"]
    assert "finalizada" in flow.send.await_args.args[1]


@pytest.mark.asyncio
async def test_sct_sin_espacio_no_consulta_arca(tmp_path, monkeypatch):
    from plugins.platforms.telegram import fiscal_query_flow as module
    instalar_documentos(monkeypatch, tmp_path, cuota="espacio")
    scripts = tmp_path / "profile/skills/productivity/sct-estado-cumplimiento/scripts"
    scripts.mkdir(parents=True)
    marker = tmp_path / "arca-consultada"
    (scripts / "sct_probe.js").write_text(
        "if(process.argv[2]==='--preflight'){console.log('result=fiscal_runtime_ready');process.exit(0);}"
        f"require('fs').writeFileSync({json.dumps(str(marker))},'1');")
    (scripts / "sct_xlsx.py").touch()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(module, "_WORKFLOW_MENU_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr("plugins.platforms.telegram.fiscal_runtime.require_fiscal_database", AsyncMock())
    access = AsyncMock(return_value=b'{"password":"x"}\n')
    monkeypatch.setattr(module, "canonical_access", access)
    flow = module.FiscalQueryFlow(catalog=NS(runtime_python=sys.executable))
    flow.send = AsyncMock()
    flow.send_document = AsyncMock()
    await flow._run_sct_dispatch(
        chat_id="1", state_key=("1", "1"), credential_line=None, credential_sha256=None,
        contributor_id=6, holder_cuit="20123456783", client_cuit="20987654321",
        client_slug="synthetic", period_mode="empty", period_from="", period_until="", period_label="")
    assert not marker.exists()
    access.assert_not_awaited()
    assert flow.send.await_args.args[1] == MENSAJE
    flow.send_document.assert_not_awaited()


# ------------------------------------------------------------ lote de resúmenes

def test_lote_usa_estado_de_ejecucion_y_no_el_arbol_de_clientes(monkeypatch, tmp_path):
    from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow
    monkeypatch.delenv("CONTABOT_BATCH_ROOT", raising=False)
    monkeypatch.delenv("CONTABOT_STATE_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert BatchPdfXlsxFlow().batch_root == tmp_path / ".local/state/contabot/recepcion-lotes"
    monkeypatch.setenv("CONTABOT_STATE_ROOT", str(tmp_path / "estado"))
    assert BatchPdfXlsxFlow().batch_root == tmp_path / "estado/recepcion-lotes"
    monkeypatch.setenv("CONTABOT_BATCH_ROOT", str(tmp_path / "explicito"))
    assert BatchPdfXlsxFlow().batch_root == tmp_path / "explicito"


def test_lote_sin_cuota_al_procesar_cierra_fallido_con_mensaje(tmp_path):
    from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow

    async def scenario():
        history = NS(start_batch=AsyncMock(return_value=NS(run_id=1, item_id=2, attempt=1)),
                     prepare_items=AsyncMock(return_value=[{"referencia": "a.pdf", "id_item": 3, "estado": "en_curso"}]),
                     finish_item=AsyncMock(), close=AsyncMock(return_value="fallida"))
        flow = BatchPdfXlsxFlow(project_dir=tmp_path, history=history)
        query = NS(answer=AsyncMock(), edit_message_text=AsyncMock())
        flow._status = AsyncMock(return_value={"digest": "a" * 64, "members": [{"member_name": "a.pdf"}]})
        flow._command = lambda *args: list(args)
        flow._run = AsyncMock(return_value={"status": "BLOCKED", "reason": "cuota_insuficiente"})
        assert await flow.callback(NS(), query, "bx:p:lote-1", 20, None, "10")
        assert query.edit_message_text.await_args.args[0] == MENSAJE
        kwargs = history.finish_item.await_args.kwargs
        assert kwargs["state"] == "fallido" and kwargs["reason_code"] == "cuota_insuficiente"
        history.close.assert_awaited_once()

    asyncio.run(scenario())


def test_lote_historial_usa_ruta_devuelta_por_documentos(tmp_path):
    from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow

    async def scenario():
        history = NS(finish_item=AsyncMock())
        flow = BatchPdfXlsxFlow(project_dir=tmp_path, history=history)
        flow.clients_root = tmp_path / "clientes"
        output = flow.clients_root / "estudios/1/20123456786/2026/05/bancos/x.xlsx"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"x")
        document = {"member_name": "a.pdf", "id_contribuyente": 9, "entity_code": "b", "currency": "ARS",
                    "account_key": "unknown", "period": "2026/05"}
        await flow._history_finish_output(
            NS(run_id=1, attempt=1), {"a.pdf": {"id_item": 3, "estado": "en_curso"}},
            {"documents": [document]},
            {"path": str(output), "ruta": "estudios/1/20123456786/2026/05/bancos/x.xlsx",
             "id_contribuyente": 9, "entity_code": "b", "currency": "ARS", "account_alias": "general"},
            already_delivered=False)
        assert history.finish_item.await_args.kwargs["output_relative"] == \
            "estudios/1/20123456786/2026/05/bancos/x.xlsx"

    asyncio.run(scenario())


# --------------------------------------------- flujos con scripts de ContaBot

def test_pdf_xlsx_cuota_muestra_mensaje_de_contabot():
    from plugins.platforms.telegram.pdf_xlsx_flow import ConversionFailure, PdfXlsxFlow
    falla = ConversionFailure("BLOCKED", "ROUTER_CUOTA_INSUFICIENTE", "r")
    assert PdfXlsxFlow._failure_message(falla) == MENSAJE
    con_payload = ConversionFailure("ERROR", "ROUTER_X", "r",
                                    documentos.mensaje_si_cuota({"codigo": "cuota_insuficiente"}))
    assert PdfXlsxFlow._failure_message(con_payload) == MENSAJE
    assert PdfXlsxFlow._failure_message(ConversionFailure("ERROR", "ROUTER_TIMEOUT", "r")) != MENSAJE


def test_agip_y_portal_iva_traducen_cuota():
    from plugins.platforms.telegram.agip_ddjj_flow import AgipDdjjFlow
    from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow
    assert AgipDdjjFlow._worker_error_message({"ok": False, "error_code": "cuota_insuficiente"}, True) == MENSAJE
    assert PortalIvaFlow._error_message({"ok": False, "motivo": "cuota_insuficiente"}, "x", "generar") == MENSAJE
    assert PortalIvaFlow._error_message({"ok": False, "motivo": "OTRO"}, "x", "generar") != MENSAJE
