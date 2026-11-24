"""#177 (turno 1906): Telegram real de DDJJ IVA contra la base efímera del gate. No es una prueba de pytest.

La lanza `database/tests/test_ddjj_tiempo_177_postgres.py` de ContaBot con el python del gateway y este checkout
en PYTHONPATH. Corre `portal_iva_ddjj_flow.run` con:
- el historial real de Operaciones (`BotRunHistory` → `bot_runs.py` → base efímera);
- el ejecutor que indique `DDJJ177_PYTHON` (el ejecutor real con ARCA simulado y reloj acelerado);
- la validación, la entrega y el cierre de ítems reales del flujo.
Se simulan sólo el bot de Telegram (mensajes, foto del captcha, documento) y la verificación de permiso e
identidad del usuario (`_scope`, `_by_id`), que este turno no cambia. Imprime un JSON con lo entregado.
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from plugins.platforms.telegram import portal_iva_ddjj_flow as ddjj
from plugins.platforms.telegram.bot_run_history import BotRunHistory
from plugins.platforms.telegram.portal_iva_flow import FlowState, PortalIvaFlow


class Mensaje:
    def __init__(self):
        self.ediciones = []

    async def edit_text(self, text, reply_markup=None):
        self.ediciones.append(text)


class Adaptador:
    def __init__(self):
        self.documentos = []
        self._bot = NS(send_message=AsyncMock(), send_photo=AsyncMock())

    async def send_document(self, chat_id, file_path, file_name, caption, metadata=None):
        datos = Path(file_path).read_bytes()
        self.documentos.append({'nombre': file_name, 'sha256': hashlib.sha256(datos).hexdigest(), 'bytes': len(datos)})
        return NS(success=True, message_id=len(self.documentos))


async def principal():
    e = os.environ
    contabot = Path(e['DDJJ177_CONTABOT'])
    escala = int(e['DDJJ177_ESCALA'])
    binding = {'id': int(e['DDJJ177_ID']), 'nombre': e['DDJJ177_NOMBRE'], 'slug': e['DDJJ177_SLUG'],
               'cuit': e['DDJJ177_CUIT'], 'study_id': int(e['DDJJ177_ESTUDIO']), 'relation_id': None,
               'relation_revision': None, 'access_id': int(e['DDJJ177_ACCESO']),
               'representative_id': int(e['DDJJ177_TITULAR'])}
    periodos = json.loads(e['DDJJ177_PERIODOS'])
    flow = PortalIvaFlow(executor=contabot / 'scripts/portal-iva/portal_iva.py',
                         clients_root=Path(e['CONTABOT_CLIENTES_ROOT']),
                         captcha_root=Path(e['CONTABOT_STATE_ROOT']) / 'portal-iva/runs',
                         runtime_python=Path(e['DDJJ177_PYTHON']),
                         history=BotRunHistory(contabot, instance_id=e['DDJJ177_INSTANCIA']))
    flow._scope = lambda: NS(require_permission=Mock(), require_actor=Mock())
    flow._by_id = lambda *a: [dict(binding)]
    # Reloj acelerado del consumidor: la misma fuente de ContaBot, dividida por la escala del ejecutor.
    real = ddjj.tiempos(flow)
    ddjj.tiempos = lambda f: NS(espera_para=lambda n: max(1, round(real.espera_para(n) / escala)),
                                reserva_para=real.reserva_para)
    state = FlowState(user_id=e['DDJJ177_TELEGRAM'], nonce=e['DDJJ177_NONCE'], stage='running',
                      operation='ddjj-iva', contributor_id=binding['id'], slug=binding['slug'], cuit=binding['cuit'],
                      nombre=binding['nombre'], period=periodos[0], ddjj_periods=periodos, scope_item=binding,
                      progress_message=Mensaje())
    adapter = Adaptador()
    inicio = time.monotonic()
    await ddjj.run(flow, adapter, 10, None, flow._key('10', None, state.user_id), state)
    textos = [c.kwargs['text'] for c in adapter._bot.send_message.await_args_list]
    print(json.dumps({'segundos': round(time.monotonic() - inicio, 1), 'documentos': adapter.documentos,
                      'fotos_captcha': adapter._bot.send_photo.await_count, 'mensajes': textos,
                      'progreso': state.progress_message.ediciones}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(principal())
    sys.stdout.flush()
