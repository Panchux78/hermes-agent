"""Real imports with another HOME/profile; no DB, browser or Telegram calls."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_contabot_defaults_follow_home_and_pdf_cache_follows_profile(tmp_path):
    home = tmp_path / "other user"
    home.mkdir()
    profile = home / "profile"
    environment = dict(os.environ, HOME=str(home), HERMES_HOME=str(profile),
                       PYTHONDONTWRITEBYTECODE="1")
    environment.pop("CONTA_PDF_XLSX_INPUT_CACHE_DIR", None)
    environment.pop("CONTA_PDF_ROUTER_PROJECT_DIR", None)
    code = """
import json
from plugins.platforms.telegram import pdf_xlsx_flow as p, portal_iva_flow as i
from plugins.platforms.telegram import agip_ddjj_flow as a, admin_maintenance_flow as m
flow = p.PdfXlsxFlow()
iva = i.PortalIvaFlow()
print(json.dumps({
 'router': str(flow.router_project_dir), 'cache': str(flow.input_cache_dir),
 'iva': str(iva.executor), 'clients': str(iva.clients_root), 'uv': str(iva.uv),
 'agip_valid': a.is_valid_delivery_path(a._CLIENTS_ROOT + '/prueba/20123456789/agip/2026/07/consultas/prueba-ddjj-iibb-agip-2026-07.xlsx'),
 'agip_other': a.is_valid_delivery_path('/not-this-home/clientes/prueba/20123456789/agip/2026/07/consultas/prueba-ddjj-iibb-agip-2026-07.xlsx'),
 'admin_repo': str(m._CONTABOT), 'admin_state': str(m._STATE_ROOT)
}))
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], env=environment,
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["router"] == str(home / "hermes-workspace/Contabot")
    assert data["cache"] == str(profile / "cache/pdf-xlsx-inputs")
    assert data["iva"] == str(home / "procedimientos/portal-iva/portal_iva.py")
    assert data["clients"] == str(home / "clientes")
    assert data["uv"] == str(profile / "bin/uv")
    assert data["agip_valid"] is True
    assert data["agip_other"] is False
    assert data["admin_repo"] == str(home / "hermes-workspace/Contabot")
    assert data["admin_state"] == str(home / ".local/state/contabot/admin")
    # Hermes puede inicializar metadata de perfil al importar plugins; nunca
    # debe crear clientes, artefactos fiscales ni rutas de otro usuario.
    assert set(home.iterdir()).issubset({profile})
    assert not (home / "clientes").exists()
    assert not (home / ".local/state/contabot").exists()
