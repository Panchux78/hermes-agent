"""Deployment paths affect real argv; no Telegram, database or fiscal calls."""
from pathlib import Path
import subprocess
import sys

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow
from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow
from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow
from plugins.platforms.telegram.agip_ddjj_flow import AgipDdjjFlow


def test_adapter_distributes_deployment_without_changing_identity(tmp_path):
    project = tmp_path / "repos/contabot"
    executor = tmp_path / "repos/procedimientos/portal-iva/portal_iva.py"
    config = PlatformConfig(enabled=True, token="fake-token", extra={
        "technical_menu_user_id": "123", "contabot": {
            "project_dir": str(project), "runtime_python": sys.executable,
            "portal_iva_executor": str(executor), "arca_map": str(tmp_path / "mapa.json"),
        }})
    adapter = TelegramAdapter(config)
    assert adapter._pdf_xlsx_flow.router_project_dir == project
    assert adapter._batch_pdf_xlsx_flow.project_dir == project
    assert adapter._portal_iva_flow.executor == executor
    assert adapter._agip_ddjj_flow.worker == project / "scripts/agip-ddjj-worker.py"
    assert adapter._admin_maintenance_flow.project_dir == project
    assert adapter._admin_maintenance_flow.arca_current == tmp_path / "mapa.json"
    for flow in (adapter._pdf_xlsx_flow, adapter._batch_pdf_xlsx_flow,
                 adapter._portal_iva_flow, adapter._agip_ddjj_flow,
                 adapter._admin_maintenance_flow):
        assert flow.runtime_python == Path(sys.executable)
    admin = adapter._admin_maintenance_flow
    from plugins.platforms.telegram.admin_maintenance_flow import _BCRA_UPDATE
    assert admin._script(_BCRA_UPDATE) == project / "database/scripts/actualizar_bancos_bcra.py"
    assert admin.python_executable == sys.executable
    assert config.token == "fake-token"
    assert config.extra["technical_menu_user_id"] == "123"
    assert not project.exists()


@pytest.mark.parametrize("settings", [
    {"runtime_python": "relative/python"}, {"runtime_python": ""},
    {"project_dir": "/tmp/../otro"}, {"portal_iva_executor": 12},
    {"python_typo": "/tmp/python"}, "not-a-mapping",
])
def test_invalid_deployment_rejected_without_fallback(settings):
    with pytest.raises(ValueError, match="CONTABOT_DEPLOYMENT_INVALID"):
        TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"contabot": settings}))


def test_router_and_batch_use_same_python_without_uv(tmp_path):
    single = PdfXlsxFlow(runtime_python=Path(sys.executable))
    batch = BatchPdfXlsxFlow(runtime_python=Path(sys.executable))
    assert single._router_command(tmp_path / "doc.pdf")[:2] == [sys.executable, "-B"]
    assert batch._command("batch-inventory")[:2] == [sys.executable, "-B"]
    command = single._router_command(tmp_path / "doc.pdf")[:2] + ["-c", "print('runtime-ok')"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert result.stdout == "runtime-ok\n"


def test_explicit_missing_python_does_not_fall_back_to_uv(tmp_path):
    flow = PdfXlsxFlow(runtime_python=tmp_path / "missing/python")
    with pytest.raises(RuntimeError, match="CONTABOT_PYTHON_UNAVAILABLE"):
        flow._router_command(tmp_path / "doc.pdf")


def test_fiscal_commands_use_selected_runtime_and_preserve_arguments(tmp_path):
    executor = tmp_path / "portal_iva.py"
    executor.touch()
    iva = PortalIvaFlow(executor=executor, clients_root=tmp_path,
                        uv=tmp_path / "missing-uv", runtime_python=Path(sys.executable))
    assert iva.available()
    argv = iva._command("cliente-sintetico", "2026-08", "descargar-presentados")
    assert argv[:4] == ["xvfb-run", "-a", sys.executable, "-B"]
    assert argv[4:] == [str(executor), "--cliente", "cliente-sintetico", "--periodo",
                         "2026-08", "--operacion", "descargar-presentados", "--captcha-stdin"]
    agip = AgipDdjjFlow(worker=tmp_path / "worker.py", runtime_python=Path(sys.executable))
    command = agip._worker_command(12, 34, "08/2026")
    assert command[-4:] == [str(tmp_path / "worker.py"), "12", "34", "08/2026"]
    assert sys.executable in command
