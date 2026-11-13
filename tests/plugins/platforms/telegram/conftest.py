"""El plugin usa el mismo SDK de prueba compartido que el gateway.

La activación añade una comprobación separada con el SDK real instalado.
"""
from tests.gateway.conftest import _ensure_telegram_mock

_ensure_telegram_mock()
