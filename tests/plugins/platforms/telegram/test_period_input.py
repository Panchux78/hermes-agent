"""Contrato de #168: sintaxis común, confirmación explícita y consumo único."""
import asyncio
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import time

import pytest

from plugins.platforms.telegram.period_input import (
    SPECS, parse_period, propose_period, route_period_callback,
)


@pytest.mark.parametrize('separator', ['-', ' - ', '–', ' – ', ' a ', ' hasta ', ' A '])
def test_range_is_always_inclusive(separator):
    result = parse_period('1/2025' + separator + '08/2025', 'comarb-lote')
    assert result.visible == tuple(f'{m:02d}/2025' for m in range(1, 9))


def test_lists_and_mixed_ranges_keep_requested_order():
    assert parse_period('08/2025, 01/2025', 'comarb-lote').visible == ('08/2025', '01/2025')
    assert parse_period('1/2025-3/2025,6/2025', 'comarb-lote').visible == (
        '01/2025', '02/2025', '03/2025', '06/2025')


@pytest.mark.parametrize('operation', SPECS)
def test_single_digit_month_is_normalized_where_admitted(operation):
    spec = SPECS[operation]
    if 'month' in spec.forms:
        assert parse_period(' 1/2025 ', operation).visible == ('01/2025',)


@pytest.mark.parametrize('operation,text,reason', [
    ('descargar-lote', '01/2025', 'rango completo'),
    ('descargar-presentados', '01/2025-02/2025', 'un solo mes'),
    ('generar', '01/2025,02/2025', 'lista'),
    ('agip-ddjj', '01/2025-02/2025', 'no admite'),
    ('ddjj-iva', '01/2023-01/2026', '36'),
    ('ddjj-iva', '02/2025-01/2025', 'anterior'),
    ('sct_estado_cumplimiento', '01/2025-01/2026', 'mismo año'),
    ('comarb-lote', '01/2025-03/2025,02/2025', 'repetidos'),
    ('comarb-periodo', '12/2009', '2010'),
    ('comarb-periodo', '11/2026', 'cerrados'),
    ('comarb-lote', '01/2025', 'no admite'),
    ('cd', '13/2025', 'MM/AAAA'),
    ('cd', '2025-01', 'MM/AAAA'),
    ('agip-ddjj', '0000', 'año'),
    ('ccma_obligaciones_pagos', '-', 'MM/AAAA'),
])
def test_rejection_explains_the_reason(operation, text, reason):
    with pytest.raises(ValueError, match=reason):
        parse_period(text, operation, today=date(2026, 11, 13))


def test_policy_limits_and_calendar_boundaries():
    assert parse_period('2025', 'agip-ddjj').summary == '2025, año completo'
    assert len(parse_period('01/2023-12/2025', 'ddjj-iva').months) == 36
    assert len(parse_period('12/2024-01/2026', 'ccma_obligaciones_pagos').months) == 14
    assert parse_period('02/2024', 'sct_estado_cumplimiento').sct() == ('range', '20240200', '20240229')
    assert parse_period('2025', 'sct_estado_cumplimiento').sct() == ('range', '20250000', '20251231')
    assert parse_period('-', 'sct_estado_cumplimiento').sct() == ('empty', '', '')
    assert len(parse_period('01/2027', 'ddjj-iva').months) == 1  # No nueva regla fiscal.
    with pytest.raises(ValueError, match='4096'):
        parse_period('1' * 4097, 'comarb-lote')
    with pytest.raises(ValueError, match='admite períodos desde 2010'):
        parse_period('12/2009', 'comarb-periodo')


def test_candidate_second_text_edit_duplicate_cancel_expiry_and_new_request():
    async def scenario():
        state = NS(created_at=time.monotonic(), nonce='request'); states = {'a': state}; send = AsyncMock(); submit = AsyncMock()
        async def offer(raw):
            await propose_period(state, 'ddjj-iva', raw, prefix='pi', send=send, submit=submit)
        async def click(action, nonce=None):
            return await route_period_callback(states, 'a', NS(answer=AsyncMock()),
                'pi:period:' + action + ':' + (nonce or state.period_candidate.nonce), 'pi')
        await offer('05/2025')
        first = state.period_candidate.nonce
        assert '1 mes' in send.await_args.args[0] and 'anteriores a 06/2025' not in send.await_args.args[0]
        submit.assert_not_awaited()
        await offer('06/2026')
        assert state.period_candidate.nonce == first
        assert 'Corregir' in send.await_args.args[0]
        await click('edit')
        await offer('05/2025-06/2026')
        assert state.period_candidate.nonce != first
        await click('yes', first)
        submit.assert_not_awaited()
        await asyncio.gather(click('yes'), click('yes'))
        submit.assert_awaited_once()
        assert len(submit.await_args.args[0].months) == 14
        # Otro pedido legítimo, mismo período: no existe deduplicación global.
        state = NS(created_at=time.monotonic(), nonce='request'); states['a'] = state
        await offer('05/2025')
        await click('cancel')
        assert states == {} and submit.await_count == 1
        state = NS(created_at=time.monotonic(), nonce='request'); states['a'] = state
        await offer('05/2025')
        state.created_at -= 601
        await click('yes')
        assert submit.await_count == 1
    asyncio.run(scenario())


def test_edit_cancel_foreign_nonce_and_ack_failure_do_not_execute():
    from telegram.error import TelegramError
    async def scenario():
        state = NS(created_at=time.monotonic(), nonce='request')
        states = {'owner': state}; send = AsyncMock(); submit = AsyncMock()
        await propose_period(state, 'cd', '01/2025', prefix='cd', send=send, submit=submit)
        nonce = state.period_candidate.nonce
        await route_period_callback(states, 'other', NS(answer=AsyncMock()), 'cd:period:yes:'+nonce, 'cd')
        submit.assert_not_awaited()
        await route_period_callback(states, 'owner', NS(answer=AsyncMock()), 'cd:period:edit:'+nonce, 'cd')
        assert state.period_candidate is None
        await route_period_callback(states, 'owner', NS(answer=AsyncMock()), 'cd:period:cancel:request', 'cd')
        assert states == {}
        states['owner'] = state
        await propose_period(state, 'cd', '01/2025', prefix='cd', send=send, submit=submit)
        await route_period_callback(states, 'owner', NS(answer=AsyncMock(side_effect=TelegramError('synthetic'))),
            'cd:period:yes:'+state.period_candidate.nonce, 'cd')
        assert states == {}
        assert 'No se inició' in send.await_args.args[0]
        submit.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['descargar-presentados', 'generar', 'descargar-lote', 'ddjj-iva', 'mis-retenciones'])
def test_portal_real_text_and_callback_require_confirmation(operation):
    from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow, FlowState
    from plugins.platforms.telegram import portal_iva_ddjj_flow
    from plugins.platforms.telegram import mis_retenciones_flow
    async def scenario():
        flow = PortalIvaFlow(); adapter = NS(_bot=NS(send_message=AsyncMock()))
        stage = ('batch_period' if operation == 'descargar-lote' else 'ddjj_period' if operation == 'ddjj-iva'
                 else 'mr_period' if operation == 'mis-retenciones' else 'period')
        state = FlowState(user_id='42', nonce='a'*10, stage=stage, operation=operation)
        key = flow._key(42, None, '42'); flow.states[key] = state
        submit = AsyncMock()
        module = mis_retenciones_flow if operation == 'mis-retenciones' else portal_iva_ddjj_flow
        if operation in ('ddjj-iva','mis-retenciones'):
            original = module.submit_period; module.submit_period = submit
        else:
            setattr(flow, '_submit_batch_period' if operation == 'descargar-lote' else '_submit_single_period', submit)
        def msg(text):return NS(chat_id=42, from_user=NS(id=42), text=text, message_thread_id=None)
        try:
            await flow.text(adapter, msg('13/2025'))
            assert getattr(state, 'period_candidate', None) is None
            valid = '01/2025-02/2025' if operation == 'descargar-lote' else '01/2025'
            await flow.text(adapter, msg(valid)); submit.assert_not_awaited()
            nonce = state.period_candidate.nonce
            await flow.text(adapter, msg('02/2025')); submit.assert_not_awaited()
            await flow.callback(adapter, NS(answer=AsyncMock()), 'pi:period:yes:'+nonce, 42, None, '42')
            submit.assert_awaited_once()
            await flow.callback(adapter, NS(answer=AsyncMock()), 'pi:period:yes:'+nonce, 42, None, '42')
            assert submit.await_count == 1
        finally:
            if operation in ('ddjj-iva','mis-retenciones'):module.submit_period = original
    asyncio.run(scenario())


@pytest.mark.parametrize('prefix', ['cd','ar','ab','tu','sf','co','dj-periodo','dj-lote'])
def test_all_deduction_and_comarb_instances_use_same_gate(prefix):
    from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow, State
    from plugins.platforms.telegram.agip_deducciones_flow import AgipDeduccionesFlow
    from plugins.platforms.telegram.provinciales_iibb import ProvincialDeduccionesFlow
    from plugins.platforms.telegram.comarb_ddjj_flow import ComarbPresentadasFlow
    async def scenario():
        classes={'cd':ComarbDeduccionesFlow, 'ar':AgipDeduccionesFlow,
                 'ab':lambda:ProvincialDeduccionesFlow('arba'), 'tu':lambda:ProvincialDeduccionesFlow('tucuman'),
                 'sf':lambda:ProvincialDeduccionesFlow('santafe'), 'co':lambda:ProvincialDeduccionesFlow('cordoba')}
        flow = ComarbPresentadasFlow() if prefix.startswith('dj') else classes[prefix]()
        modo = prefix.split('-')[1] if prefix.startswith('dj') else 'uno'
        state = State('42','a'*8,'periodo',modo,contributor={'id':1,'nombre':'Sintético'})
        key=flow._key(42,None,'42'); flow.states[key]=state
        flow._submit_period=AsyncMock(); adapter=NS(_bot=NS(send_message=AsyncMock()))
        def msg(text):return NS(chat_id=42, from_user=NS(id=42), text=text, reply_text=AsyncMock())
        await flow.text(adapter,msg('13/2025'))
        assert getattr(state,'period_candidate',None) is None
        await flow.text(adapter,msg('1/2025, 3/2025' if modo=='lote' else '1/2025'))
        flow._submit_period.assert_not_awaited()
        cb=f'{flow.callback_prefix}:period:yes:{state.period_candidate.nonce}'
        await flow.callback(adapter,NS(answer=AsyncMock()),cb,42,None,'42')
        flow._submit_period.assert_awaited_once()
        await flow.callback(adapter,NS(answer=AsyncMock()),cb,42,None,'42')
        assert flow._submit_period.await_count==1
    asyncio.run(scenario())


@pytest.mark.parametrize('operation',['ccma_obligaciones_pagos','sct_estado_cumplimiento'])
def test_fiscal_real_text_and_callback_share_gate(operation):
    from plugins.platforms.telegram.fiscal_query_flow import FiscalQueryFlow, _WorkflowMenuState
    async def scenario():
        flow=FiscalQueryFlow(); adapter=NS(_bot=NS(send_message=AsyncMock()), send=AsyncMock(),
            send_document=AsyncMock(),_background_tasks=set())
        flow.bind(adapter)
        state=_WorkflowMenuState(skill_command=operation,label='Sintético',user_id='42',stage='period')
        flow._workflow_menu_state[('42','42')]=state; flow._submit_period=AsyncMock()
        def msg(text):return NS(chat=NS(id=42,type='private'),from_user=NS(id=42),text=text)
        await flow.text(adapter,msg('13/2025')); assert getattr(state,'period_candidate',None) is None
        await flow.text(adapter,msg('2025')); flow._submit_period.assert_not_awaited()
        cb='fq:period:yes:'+state.period_candidate.nonce
        await flow.callback(adapter,NS(answer=AsyncMock()),cb,42,None,'42')
        flow._submit_period.assert_awaited_once()
    asyncio.run(scenario())


def test_agip_real_text_and_callback_share_gate():
    from plugins.platforms.telegram.agip_ddjj_flow import AgipDdjjFlow, FlowState
    async def scenario():
        flow=AgipDdjjFlow(); adapter=NS(_bot=NS(send_message=AsyncMock()))
        state=FlowState(user_id='42',nonce='a'*10,stage='period')
        flow.states[flow._key(42,None,42)]=state; flow._submit_period=AsyncMock()
        def msg(text):return NS(chat_id=42,from_user=NS(id=42),text=text)
        await flow.text(adapter,msg('13/2025')); assert getattr(state,'period_candidate',None) is None
        await flow.text(adapter,msg('2025')); flow._submit_period.assert_not_awaited()
        await flow.callback(adapter,NS(answer=AsyncMock()),'ad:period:yes:'+state.period_candidate.nonce,42,None,'42')
        flow._submit_period.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['05/01/2025-17/02/2025','5/1/2025 – 17/2/2025','05/01/2025 a 17/02/2025'])
def test_mis_retenciones_keeps_exact_days(text):
    result = parse_period(text,'mis-retenciones',today=date(2026,11,13))
    assert result.dates()==('05/01/2025','17/02/2025')
    assert result.summary=='05/01/2025 a 17/02/2025, 44 días'
    assert parse_period('2/2024','mis-retenciones').dates()==('01/02/2024','29/02/2024')
    assert parse_period('01/2099','mis-retenciones').dates()==('01/01/2099','31/01/2099')
    for invalid,reason in [('31/02/2025-01/03/2025','no existe'),('02/01/2025-01/01/2025','anterior'),
                           ('01/01/2025-14/11/2026','posterior a hoy')]:
        with pytest.raises(ValueError,match=reason):
            parse_period(invalid,'mis-retenciones',today=date(2026,11,13))
