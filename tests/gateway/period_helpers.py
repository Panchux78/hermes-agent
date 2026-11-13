"""Confirmación explícita del candidato para las regresiones de los ejecutores."""
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def confirm_period(flow, adapter, chat='10', user='7', prefix=None):
    if hasattr(flow, '_workflow_menu_state'):
        state = flow._workflow_menu_state[(str(chat), str(user))]; prefix = 'fq'
    else:
        state = flow.states[flow._key(chat, None, user)]
        prefix = prefix or getattr(flow, 'callback_prefix', None) or ('pi' if hasattr(state, 'operation') else 'ad')
    assert state.period_candidate is not None
    query = SimpleNamespace(answer=AsyncMock())
    await flow.callback(adapter, query, f'{prefix}:period:yes:{state.period_candidate.nonce}', chat, None, user)
