"""Constancia pública: adaptador Telegram del motor de ARCA compartido."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def _shared_arca():
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "skills/shared/arca"
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise RuntimeError("constancia_navegador_compartido_no_disponible")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _arca_browser():
    _shared_arca()
    from arca_browser import firefox
    return firefox()


_shared_arca()
from constancia_pdf_motor import (FORM, CaptchaRechazado, archivar_pdf, flujo_documento,
                                 ConstanciaPdfSession as _Session, LecturaAutomatica as _Auto)


class ConstanciaPdfSession(_Session):
    def __init__(self):
        super().__init__(lambda: _arca_browser())


class LecturaAutomatica(_Auto):
    def __init__(self):
        super().__init__(preparar=_shared_arca)
