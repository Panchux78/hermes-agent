"""Procedencia por tarea, no por usuario ni variable global compartida."""
from contextvars import ContextVar
from functools import wraps
import os
import re

_origin = ContextVar('contabot_execution_origin', default=None)


def environment(base=None):
    env = dict(os.environ if base is None else base)
    origin = _origin.get() or ('prueba', env.get('CONTABOT_PRUEBA_REFERENCIA', 'ejecucion_tecnica'), '', '-')
    canal, proceso, entrada, actor = origin
    if canal not in ('telegram', 'prueba','automatico') or not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,120}', proceso or 'persona') or not re.fullmatch(r'[a-zA-Z0-9_.:/-]{0,160}', entrada) or not re.fullmatch(r'[0-9]{1,20}|-',str(actor)):
        raise ValueError('procedencia_no_valida')
    values = {'canal': canal, 'proceso': proceso or '-', 'entrada': entrada or '-', 'actor':str(actor)}
    env['PGOPTIONS'] = env.get('PGOPTIONS', '') + ''.join(' -c contabot.'+k+'='+v for k,v in values.items())
    for k,v in values.items():env['CONTABOT_ORIGEN_'+k.upper()] = v
    return env


def incoming(callback):
    """Sólo se instala en los handlers PTB. Llamar un flujo a mano es prueba."""
    @wraps(callback)
    async def wrapped(update, context):
        uid = getattr(update, 'update_id', None)
        message = getattr(update, 'effective_message', None)
        query = getattr(update, 'callback_query', None)
        actor = getattr(query, 'from_user', None) if query else getattr(message, 'from_user', None)
        if type(uid) is not int or uid < 0 or actor is None:
            origin = ('prueba', 'update_no_acreditado', '', '-')
        else:
            mid = getattr(message, 'message_id', None)
            origin = ('telegram', '', f'update:{uid}/'+('callback' if query else f'mensaje:{mid}'), str(actor.id))
        token = _origin.set(origin)
        try:return await callback(update, context)
        finally:_origin.reset(token)
    return wrapped


async def programmed(coroutine, proceso):
    token = _origin.set(('automatico',proceso,'','-'))
    try:return await coroutine
    finally:_origin.reset(token)
