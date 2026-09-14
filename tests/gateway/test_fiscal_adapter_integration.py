"""Production-menu topology, real adapter/flow; no live Telegram or ARCA."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    # The repository conftest installs a generic Telegram mock; preserve button
    # values here, rather than asserting against its empty MagicMock iterator.
    import plugins.platforms.telegram.adapter as module
    monkeypatch.setattr(module, 'InlineKeyboardButton', lambda text, callback_data: NS(text=text, callback_data=callback_data))
    monkeypatch.setattr(module, 'InlineKeyboardMarkup', lambda rows: NS(inline_keyboard=rows))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    a = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic', extra={
        'contabot': {'runtime_python': sys.executable}}))
    a._bot = NS(send_message=AsyncMock())
    a.send = AsyncMock()
    a._is_user_authorized_from_message = lambda m: m.from_user.id == 7
    a._is_callback_user_authorized = lambda uid, **kw: uid == '7'
    a._log_blocked_user = Mock()
    return a


def query(action, uid=7, chat_type='private'):
    return NS(data=action, from_user=NS(id=uid, first_name='Synthetic'),
        message=NS(chat_id=7, chat=NS(type=chat_type), message_thread_id=None),
        answer=AsyncMock(), edit_message_text=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ccma', 'sct'])
async def test_real_menu_to_canonical_identity_period_and_dispatch(adapter, action):
    rows = adapter._menu_panel_keyboard('arca_consultar').inline_keyboard
    buttons = {b.callback_data: b for row in rows for b in row}
    label = 'CCMA Obligaciones y pagos' if action == 'ccma' else 'SCT Estado de cumplimiento'
    assert buttons[f'fq:{action}'].text == menu_label('📊' if action == 'ccma' else '📋', label)
    assert 'pi:descargar' in buttons
    assert all(len(row) == 1 for row in rows[:-1])
    assert [b.callback_data for b in rows[-1]] == ['om:arca', 'om:close']
    flow = adapter._fiscal_query_flow
    assert flow.catalog is not adapter._portal_iva_flow
    assert flow.catalog.runtime_python == Path(sys.executable)
    assert adapter._portal_iva_flow.query_connection is None
    row = {'id': 3, 'nombre': 'Cliente sintético', 'cuit': '20987654321', 'slug': 'cliente-sintetico'}
    flow.catalog._search = Mock(return_value=[row])
    flow.catalog._by_id = Mock(return_value=[row])
    flow.catalog._query = Mock(return_value=[{'usuario': '20123456783'}])
    flow._start_ccma_dispatch = AsyncMock()
    flow._start_sct_dispatch = AsyncMock()
    adapter._enqueue_text_event = Mock(side_effect=AssertionError('must not invoke agent'))
    await adapter._handle_callback_query(NS(callback_query=query(f'fq:{action}')), None)
    for text in ('cliente-sintetico', '08/2026'):
        message = NS(text=text, chat_id=7, chat=NS(id=7, type='private'), from_user=NS(id=7),
                     message_thread_id=None, reply_to_message=None)
        await adapter._handle_text_message(NS(effective_message=message), NS())
    dispatch = flow._start_ccma_dispatch if action == 'ccma' else flow._start_sct_dispatch
    assert dispatch.await_count == 1
    args = dispatch.await_args.kwargs
    assert args['contributor_id'] == 3
    assert args['holder_cuit'] == '20123456783'
    assert args['client_cuit'] == '20987654321'
    assert args['period_from'] == ('08/2026' if action == 'ccma' else '20260800')
    nonce = flow._workflow_menu_state[('7', '7')].nonce
    task = asyncio.create_task(asyncio.Event().wait())
    flow._sct_dispatch_tasks[('7', '7')] = task
    await adapter._handle_callback_query(NS(callback_query=query(f'fq:cancel:{nonce}')), None)
    assert task.cancelled() and not flow._workflow_menu_state


@pytest.mark.asyncio
@pytest.mark.parametrize('uid,chat_type', [(8, 'private'), (7, 'group')])
async def test_forbidden_fiscal_callback_does_not_create_state(adapter, uid, chat_type):
    q = query('fq:ccma', uid, chat_type)
    await adapter._handle_callback_query(NS(callback_query=q), None)
    q.answer.assert_awaited_once()
    assert not adapter._fiscal_query_flow._workflow_menu_state


def test_restricted_lookup_uses_portable_profile_and_never_falls_back(adapter, monkeypatch):
    import plugins.platforms.telegram.portal_iva_flow as module
    from plugins.platforms.telegram import fiscal_credentials
    seen = []
    def invocation(capability):
        seen.append(capability)
        return ['restricted-psql'], {'PGPASSFILE': '/synthetic-only'}
    monkeypatch.setitem(sys.modules, 'contabot_pg', NS(psql_invocation=invocation))
    runner = Mock(return_value=NS(returncode=0, stdout=''))
    monkeypatch.setattr(module.subprocess, 'run', runner)
    adapter._fiscal_query_flow.catalog._search('synthetic')
    assert seen == ['lookup']
    assert runner.call_args.args[0][0] == 'restricted-psql'
    assert runner.call_args.kwargs['env'] == {'PGPASSFILE': '/synthetic-only'}
    monkeypatch.setitem(sys.modules, 'contabot_pg', None)
    runner.reset_mock()
    with pytest.raises(ImportError):
        adapter._fiscal_query_flow.catalog._search('synthetic')
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_revoked_caller_cannot_answer_current_captcha(adapter):
    from plugins.platforms.telegram.fiscal_query_flow import _WorkflowMenuState
    flow = adapter._fiscal_query_flow
    future = asyncio.get_running_loop().create_future()
    flow._workflow_menu_state[('7', '7')] = _WorkflowMenuState(
        skill_command='ccma_obligaciones_pagos', label='CCMA', stage='captcha', captcha_response=future)
    adapter._is_user_authorized_from_message = lambda m: False
    message = NS(text='ABCD', chat=NS(id=7, type='private'), from_user=NS(id=7))
    await adapter._handle_text_message(NS(effective_message=message), NS())
    assert not future.done()


@pytest.mark.asyncio
async def test_disconnect_cancels_fiscal_tasks_and_clears_state(adapter):
    flow = adapter._fiscal_query_flow
    task = asyncio.create_task(asyncio.Event().wait())
    flow._sct_dispatch_tasks[('7','7')] = task
    flow._workflow_menu_state[('7','7')] = object()
    await adapter.disconnect()
    assert task.cancelled() and not flow._workflow_menu_state


@pytest.mark.asyncio
@pytest.mark.parametrize('existing,new', [('portal','fq:ccma'), ('fiscal','pi:generar'), ('fiscal','ad:start')])
async def test_new_fiscal_wiring_never_intercepts_another_active_flow(adapter, existing, new):
    if existing == 'portal':
        key = adapter._portal_iva_flow._key(7, None, 7)
        adapter._portal_iva_flow.states[key] = object()
    else:
        adapter._fiscal_query_flow._workflow_menu_state[('7','7')] = object()
    q = query(new)
    await adapter._handle_callback_query(NS(callback_query=q), None)
    assert 'cancelá' in q.answer.await_args.kwargs['text']
    q.edit_message_text.assert_not_awaited()
    adapter._bot.send_message.assert_not_awaited()
