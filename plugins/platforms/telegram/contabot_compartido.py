"""Motores compartidos con el panel (#200): un solo archivo para los dos canales.

El despliegue de ContaBot publica en /srv/contabot-console/integrations/ los módulos de lib/ que usan
el bot y la consola (Proteger/Desbloquear PDF, Excel/ICS de Vencimientos). El bot los carga desde ahí,
igual que ya ejecuta regenerar_vencimientos_arca.py; la consola importa los mismos archivos.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def directorio() -> Path:
    return Path(os.environ.get("CONTABOT_INTEGRATIONS_DIR", "/srv/contabot-console/integrations"))


def ruta(nombre: str) -> Path:
    return directorio() / f"{nombre}.py"


def cargar(nombre: str):
    """Importa el módulo publicado; sin él la función no está disponible (no hay copia de respaldo)."""
    path = ruta(nombre)
    cached = sys.modules.get(nombre)
    if cached is not None and Path(getattr(cached, "__file__", "")) == path:
        return cached
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"motor_compartido_no_disponible:{nombre}")
    spec = importlib.util.spec_from_file_location(nombre, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"motor_compartido_no_disponible:{nombre}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = module
    spec.loader.exec_module(module)
    return module
