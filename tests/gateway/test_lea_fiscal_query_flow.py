"""Private fiscal menu dispatch, with synthetic taxpayer data only."""
import asyncio
import hashlib
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.platforms.event import MessageType
from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow


@pytest.fixture
def flow(monkeypatch, tmp_path):
    # These tests isolate menu/dispatch; real preflight has its own subprocess suite.
    monkeypatch.setattr('plugins.platforms.telegram.fiscal_query_flow.require_fiscal_runtime', AsyncMock())
    monkeypatch.setattr('plugins.platforms.telegram.fiscal_query_flow.verify_representation', AsyncMock())
    monkeypatch.setenv('CONTABOT_CLIENTES_ROOT', str(tmp_path/'clientes'))
    row = {
        'id': 3, 'nombre': 'Cliente de prueba', 'cuit': '20123456786', 'slug': 'cliente-prueba',
        'study_id': 1, 'relation_id': 31, 'relation_revision': 1,
        'verified': False, 'representative_id': 3, 'holder_cuit': '20123456786',
    }
    scope = NS(require_actor=Mock(return_value=None))
    catalog = NS(_search=Mock(return_value=[row]), _by_id=Mock(return_value=[row]),
                 _query=Mock(return_value=[{'usuario': '20123456786'}]), _visible_cuit=lambda c: c,
                 _scope=lambda: scope)
    catalog.runtime_python = sys.executable
    f = FiscalQueryFlow(catalog=catalog)
    adapter = NS(send=AsyncMock(), send_document=AsyncMock(), handle_message=AsyncMock(),
                 _bot=NS(send_message=AsyncMock()), _background_tasks=set(), _build_message_event=Mock(return_value=NS(text='')))
    f.bind(adapter)
    return f


def message(text, uid=7):
    return NS(text=text, chat=NS(id=7, type='private'), from_user=NS(id=uid))


async def start(flow, action):
    q = NS(answer=AsyncMock(), edit_message_text=AsyncMock())
    await flow.callback(flow._adapter, q, f'fq:{action}' if action != 'cancel' else f'fq:cancel:{flow._workflow_menu_state[("7", "7")].nonce}', '7', None, '7')
    return q


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ccma', 'sct'])
async def test_actor_database_failure_is_reported_without_starting_state(flow, action):
    flow.catalog._scope().require_actor.side_effect = RuntimeError('private database detail')

    query = await start(flow, action)

    assert not flow._workflow_menu_state
    query.answer.assert_awaited_once_with('Consulta no disponible')
    assert flow.send.await_args.args[1] == (
        'No pude consultar la base de ContaBot. Probá nuevamente en unos minutos.'
    )
    assert 'private database detail' not in flow.send.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ccma', 'sct'])
async def test_selection_period_and_private_dispatch(flow, action):
    flow._resolve_ccma_credential_line = AsyncMock(return_value=3)
    flow._resolve_sct_credential_line = AsyncMock(return_value=(3, 'a' * 64))
    flow._start_sct_dispatch = AsyncMock()
    flow._start_ccma_dispatch = AsyncMock()
    await start(flow, action)
    assert not await flow.text(flow._adapter, message('20123456786', uid=8))
    for text in ('cliente-prueba', 'invalid'):
        assert await flow.text(flow._adapter, message(text))
    flow._adapter.handle_message.assert_not_awaited()
    flow._start_sct_dispatch.assert_not_awaited()
    assert await flow.text(flow._adapter, message('2026'))
    flow._adapter.handle_message.assert_not_awaited()
    dispatch = flow._start_ccma_dispatch if action == 'ccma' else flow._start_sct_dispatch
    kwargs = dispatch.await_args.kwargs
    assert kwargs['credential_sha256'] is None
    assert kwargs['contributor_id'] == 3
    assert kwargs['holder_cuit'] == '20123456786'
    flow._resolve_sct_credential_line.assert_not_awaited()
    assert kwargs['period_from'] == ('01/2026' if action == 'ccma' else '20260000')
    if action == 'sct':
        assert kwargs['client_slug'] == 'cliente-prueba'
        assert kwargs['client_cuit'] == '20123456786'



@pytest.mark.asyncio
async def test_callback_cancel_and_disconnect_stop_tasks(flow):
    await start(flow, 'sct')
    task = asyncio.create_task(asyncio.Event().wait())
    flow._sct_dispatch_tasks[('7', '7')] = task
    await start(flow, 'cancel')
    assert task.cancelled()
    assert not flow._workflow_menu_state
    task = asyncio.create_task(asyncio.Event().wait())
    flow._sct_dispatch_tasks[('7', '7')] = task
    await flow.close()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_repeated_selection_does_not_replace_private_state(flow):
    await start(flow, 'ccma')
    await flow.text(flow._adapter, message('20123456783'))
    q = await start(flow, 'sct')
    q.edit_message_text.assert_not_awaited()
    assert flow._workflow_menu_state[('7', '7')].skill_command == 'ccma_obligaciones_pagos'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ccma', 'sct'])
@pytest.mark.parametrize('failed_call', ['answer', 'edit_message_text'])
async def test_failed_start_releases_state_and_allows_next_click(flow, action, failed_call):
    from telegram.error import NetworkError

    q = NS(answer=AsyncMock(), edit_message_text=AsyncMock())
    getattr(q, failed_call).side_effect = NetworkError('synthetic connection failure')
    with pytest.raises(NetworkError):
        await flow.callback(flow._adapter, q, f'fq:{action}', '7', None, '7')
    assert ('7', '7') not in flow._workflow_menu_state
    assert not flow._sct_dispatch_tasks
    assert not flow._background_tasks
    q = await start(flow, action)
    q.edit_message_text.assert_awaited_once()
    assert flow._workflow_menu_state[('7', '7')].stage == 'client'


@pytest.mark.asyncio
async def test_cancelled_start_releases_only_its_own_state(flow):
    from plugins.platforms.telegram.fiscal_query_flow import _WorkflowMenuState

    q = NS(answer=AsyncMock(side_effect=asyncio.CancelledError), edit_message_text=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await flow.callback(flow._adapter, q, 'fq:ccma', '7', None, '7')
    assert ('7', '7') not in flow._workflow_menu_state

    replacement = _WorkflowMenuState(skill_command='sct_estado_cumplimiento', label='SCT')
    async def replace_then_cancel():
        flow._workflow_menu_state[('7', '7')] = replacement
        raise asyncio.CancelledError
    q.answer.side_effect = replace_then_cancel
    with pytest.raises(asyncio.CancelledError):
        await flow.callback(flow._adapter, q, 'fq:ccma', '7', None, '7')
    assert flow._workflow_menu_state[('7', '7')] is replacement


@pytest.mark.asyncio
async def test_failed_start_does_not_clear_query_already_advanced(flow):
    from telegram.error import NetworkError

    async def advance_then_fail(*args, **kwargs):
        flow._workflow_menu_state[('7', '7')].stage = 'running'
        raise NetworkError('synthetic prompt delivery failure')
    q = NS(answer=AsyncMock(), edit_message_text=AsyncMock(side_effect=advance_then_fail))
    with pytest.raises(NetworkError):
        await flow.callback(flow._adapter, q, 'fq:ccma', '7', None, '7')
    assert flow._workflow_menu_state[('7', '7')].stage == 'running'

from unittest.mock import MagicMock
from types import SimpleNamespace
import plugins.platforms.telegram.fiscal_query_flow as fiscal_module


@pytest.mark.asyncio
async def test_sct_dispatcher_uses_only_opaque_runner_environment(flow, monkeypatch, tmp_path):
    class FakeProcess:
        returncode = 0
        communicate = AsyncMock(return_value=(b"result=sct_handoff_login_not_verified\n", b""))

    process = FakeProcess()
    create_process = AsyncMock(return_value=process)
    hermes_home = tmp_path / "hermes"
    probe = hermes_home / "skills" / "productivity" / "sct-estado-cumplimiento" / "scripts" / "sct_probe.js"
    xlsx_builder = probe.with_name("sct_xlsx.py")
    probe.parent.mkdir(parents=True)
    probe.touch()
    xlsx_builder.touch()
    uv = hermes_home / "bin" / "uv"
    uv.parent.mkdir()
    uv.touch()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    access = hermes_home / '.arca.csv'
    access.write_bytes(b'synthetic-only')
    access.chmod(0o600)
    credential_sha256 = hashlib.sha256(access.read_bytes()).hexdigest()
    monkeypatch.setattr(fiscal_module, 'terminate_owned_group', AsyncMock())
    monkeypatch.setattr(
        fiscal_module.shutil,
        "which",
        lambda name: {"node": "/fake/node", "uv": "/fake/uv"}.get(name),
    )
    monkeypatch.setattr(fiscal_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        flow,
        "_sct_dispatch_paths",
        MagicMock(
            return_value=(
                tmp_path / "result.xlsx",
                tmp_path / "private" / "source.csv",
                tmp_path / "private" / "login.png",
                tmp_path / "private" / "service.png",
                tmp_path / "private" / "result.png",
            )
        ),
    )

    await flow._run_sct_dispatch(
        chat_id="123",
        state_key=("123", "456"),
        credential_line=3,
        credential_sha256=credential_sha256,
        period_mode="range",
        period_from="20260000",
        period_until="20261231",
        period_label="2026",
        client_slug="cliente-prueba", client_cuit="20123456786",
    )

    create_process.assert_awaited_once()
    command = create_process.await_args.args
    environment = create_process.await_args.kwargs["env"]
    assert command[0:2] == ("/fake/node", str(probe))
    assert environment["ARCA_CSV_LINE"] == "3"
    assert environment["ARCA_CSV_SHA256"] == credential_sha256
    assert environment['ARCA_CSV_FILE'] == str(tmp_path / 'private/source.access.csv')
    assert environment['HERMES_HOME'] == str(hermes_home)
    assert not (tmp_path / 'private/source.access.csv').exists()
    assert environment["SCT_PERIOD_MODE"] == "range"
    assert environment["SCT_PERIOD_FROM"] == "20260000"
    assert environment["SCT_PERIOD_UNTIL"] == "20261231"
    assert set(environment) == {
        "HOME",
        "HERMES_HOME",
        "ARCA_CSV_FILE",
        "PATH",
        "LANG",
        "ARCA_CSV_LINE",
        "ARCA_CSV_SHA256",
        "SCT_PERIOD_MODE",
        "SCT_PERIOD_FROM",
        "SCT_PERIOD_UNTIL",
        "SCT_EXPORT_FILE",
        "SCT_LOGIN_FAILURE_SCREENSHOT",
        "SCT_SERVICE_FAILURE_SCREENSHOT",
        "SCT_RESULT_SCREENSHOT",
        "FISCAL_CAPTCHA_DIR",
        "FISCAL_RUNTIME_PYTHON",
    }
    assert not any(key.startswith("SCT_REQUESTED_") for key in environment)
    assert not any("CUIT" in key or "PASSWORD" in key or "CONTRASE" in key for key in environment)
    flow._adapter.handle_message.assert_not_awaited()
    flow.send.assert_awaited_once_with(
        "123",
        "La consulta SCT terminó sin exportación: `sct_handoff_login_not_verified`. Referencia: source-captcha. Revisá la evidencia privada.",
    )



@pytest.mark.asyncio
async def test_sct_dispatcher_builds_and_delivers_xlsx_without_model(flow, monkeypatch, tmp_path):
    class FakeProcess:
        def __init__(self, stdout=b"", on_communicate=None):
            self.returncode = 0
            self._stdout = stdout
            self._on_communicate = on_communicate

        async def communicate(self):
            if self._on_communicate is not None:
                await self._on_communicate()
            return self._stdout, b""

    hermes_home = tmp_path / "hermes"
    probe = hermes_home / "skills" / "productivity" / "sct-estado-cumplimiento" / "scripts" / "sct_probe.js"
    xlsx_builder = probe.with_name("sct_xlsx.py")
    probe.parent.mkdir(parents=True)
    probe.touch()
    xlsx_builder.touch()
    uv = hermes_home / "bin" / "uv"
    uv.parent.mkdir()
    uv.touch()
    xlsx_file = tmp_path / "result.xlsx"

    async def create_xlsx():
        xlsx_file.write_bytes(b"fake-xlsx")

    create_process = AsyncMock(
        side_effect=[
            FakeProcess(b"result=sct_exported\n"),
            FakeProcess(on_communicate=create_xlsx),
        ]
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    access = hermes_home / '.arca.csv'
    access.write_bytes(b'synthetic-only')
    access.chmod(0o600)
    credential_sha256 = hashlib.sha256(access.read_bytes()).hexdigest()
    monkeypatch.setattr(fiscal_module, 'terminate_owned_group', AsyncMock())
    monkeypatch.setattr(
        fiscal_module.shutil,
        "which",
        lambda name: {"node": "/fake/node", "uv": "/fake/uv"}.get(name),
    )
    monkeypatch.setattr(fiscal_module.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(
        flow,
        "_sct_dispatch_paths",
        MagicMock(
            return_value=(
                xlsx_file,
                tmp_path / "private" / "source.csv",
                tmp_path / "private" / "login.png",
                tmp_path / "private" / "service.png",
                tmp_path / "private" / "result.png",
            )
        ),
    )
    flow.send_document = AsyncMock(return_value=SimpleNamespace(success=True))

    await flow._run_sct_dispatch(
        chat_id="123",
        state_key=("123", "456"),
        credential_line=3,
        credential_sha256=credential_sha256,
        period_mode="range",
        period_from="20260000",
        period_until="20261231",
        period_label="2026",
        client_slug="cliente-prueba", client_cuit="20123456786",
        telegram_id="7",
        scope_item={
            'id': 3, 'cuit': '20123456786', 'relation_id': 31,
            'relation_revision': 1, 'holder_cuit': '20123456786',
        },
    )

    assert create_process.await_count == 2
    assert create_process.await_args_list[1].args[:2] == (sys.executable, "-B")
    published = tmp_path/'clientes/cliente-prueba/20123456786/arca/2026/anual/consultas/cliente-prueba-sct-estado-cumplimiento-arca-2026.xlsx'
    flow.send_document.assert_awaited_once_with(
        chat_id="123",
        file_path=str(published),
        file_name=published.name,
        caption="Estado de Cumplimiento SCT — consulta read-only.",
    )
    flow._adapter.handle_message.assert_not_awaited()
    flow.send.assert_awaited_once_with(
        "123",
        "Consulta SCT finalizada. Se entregó el XLSX; la fuente y evidencias quedan privadas.",
    )


@pytest.mark.asyncio
async def test_multiple_candidates_require_current_offered_selection(flow, monkeypatch):
    monkeypatch.setattr(fiscal_module, 'InlineKeyboardButton', lambda text, callback_data: NS(text=text, callback_data=callback_data))
    monkeypatch.setattr(fiscal_module, 'InlineKeyboardMarkup', lambda rows: NS(inline_keyboard=rows))
    flow.catalog._search.return_value = [
        {'id': 3, 'nombre': 'Uno', 'cuit': '20123456786', 'slug': 'uno'},
        {'id': 4, 'nombre': 'Dos', 'cuit': '20222222223', 'slug': 'dos'}]
    flow._resolve_sct_credential_line = AsyncMock(return_value=(3, 'a'*64))
    await start(flow, 'sct')
    await flow.text(flow._adapter, message('empresa'))
    state=flow._workflow_menu_state[('7','7')]
    assert state.stage == 'client'
    keyboard=flow._adapter._bot.send_message.await_args.kwargs['reply_markup']
    assert len(keyboard.inline_keyboard) == 3
    q=NS(answer=AsyncMock(),edit_message_text=AsyncMock())
    for data in (f'fq:select:oldnonce:3', f'fq:select:{state.nonce}:999'):
        await flow.callback(flow._adapter,q,data,'7',None,'7')
    flow._resolve_sct_credential_line.assert_not_awaited()
    await flow.callback(flow._adapter,q,f'fq:select:{state.nonce}:3','7',None,'7')
    assert state.stage == 'period'
    flow._resolve_sct_credential_line.assert_not_awaited()
    assert state.holder_cuit == '20123456786'


@pytest.mark.asyncio
async def test_old_cancel_cannot_cancel_new_request(flow):
    await start(flow, 'ccma')
    old=flow._workflow_menu_state[('7','7')].nonce
    await start(flow,'cancel')
    await start(flow,'sct')
    current=flow._workflow_menu_state[('7','7')]
    q=NS(answer=AsyncMock(),edit_message_text=AsyncMock())
    await flow.callback(flow._adapter,q,f'fq:cancel:{old}','7',None,'7')
    assert flow._workflow_menu_state[('7','7')] is current
    q.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_removed_access_blocks_execution_after_period(flow):
    flow._resolve_sct_credential_line = AsyncMock(return_value=(3, 'a'*64))
    flow._start_sct_dispatch=AsyncMock()
    await start(flow,'sct')
    await flow.text(flow._adapter,message('cliente-prueba'))
    flow.catalog._by_id.return_value=[]
    await flow.text(flow._adapter,message('2026'))
    flow._start_sct_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_match_or_database_failure_keeps_search_stage(flow):
    await start(flow,'ccma')
    flow.catalog._search.return_value=[]
    await flow.text(flow._adapter,message('desconocido'))
    assert flow._workflow_menu_state[('7','7')].stage=='client'
    flow.catalog._search.side_effect=RuntimeError('private database detail')
    await flow.text(flow._adapter,message('otro'))
    assert 'private database detail' not in flow.send.await_args.args[1]
    flow._adapter.handle_message.assert_not_awaited()
