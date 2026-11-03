"""DDJJ IVA presentada: contrato independiente, transporte/selector Portal IVA comunes."""
from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree
from telegram.error import TelegramError

from plugins.platforms.telegram.fiscal_credentials import verify_representation

OPERATION = 'portal_iva_ddjj_iva_excel'
PROMPT = 'Ingresá MM/AAAA o MM/AAAA a MM/AAAA (máximo 12 meses). Ejemplo: 09/2026.'
_MONTH = r'(0[1-9]|1[0-2])/[0-9]{4}'


def periods(text):
    if not re.fullmatch(rf'{_MONTH}( a {_MONTH})?', text):
        raise ValueError('DDJJ_PERIODO_INVALIDO')
    parts = text.split(' a ')
    start, end = parts[0], parts[-1]
    first = int(start[3:]) * 12 + int(start[:2]) - 1
    last = int(end[3:]) * 12 + int(end[:2]) - 1
    if not 0 <= last - first < 12:
        raise ValueError('DDJJ_RANGO_INVALIDO')
    return [f'{n//12:04d}-{n%12+1:02d}' for n in range(first, last+1)]


def command(flow, state):
    executor = flow.executor.with_name('portal_iva_ddjj.py')
    if not flow.available() or not executor.is_file():
        raise RuntimeError('DDJJ_EJECUTOR_NO_DISPONIBLE')
    binding = state.scope_item or {}
    if not binding.get('access_id') or not binding.get('representative_id'):
        raise RuntimeError('DDJJ_ACCESO_NO_DISPONIBLE')
    args = [str(flow.uv), 'run', '--no-project', '--with', 'selenium', '--with', 'openpyxl',
            'xvfb-run', '-a', 'python3', str(executor), '--cliente', state.slug,
            '--id-acceso', str(int(binding['access_id'])), '--id-titular', str(int(binding['representative_id'])), '--captcha-stdin']
    for period in state.ddjj_periods:
        args += ['--periodo', period]
    return args


def progress(stage):
    labels = {'debito_credito': 'Leyendo débito y crédito fiscal', 'retenciones': 'Leyendo retenciones',
              'impositivas': 'Leyendo percepciones impositivas', 'aduaneras': 'Leyendo percepciones aduaneras',
              'saldos': 'Leyendo administración de saldos', 'determinacion': 'Leyendo determinación del impuesto'}
    match = re.fullmatch(r'ddjj:((?:0[1-9]|1[0-2])/[0-9]{4}):([a-z_]+)', stage)
    return f'{match[1]} · {labels[match[2]]}…' if match and match[2] in labels else None


async def receive_period(flow, adapter, message, state, key):
    chat, thread = message.chat_id, getattr(message, 'message_thread_id', None)
    try:
        state.ddjj_periods = periods((message.text or '').strip())
    except ValueError:
        await flow._send(adapter, chat, PROMPT, thread)
        return True
    state.period = state.ddjj_periods[0]
    state.stage = 'running'
    state.progress_label = f'{state.nombre} · Buscando DDJJ de IVA presentadas…'
    state.progress_message = await flow._send(adapter, chat, state.progress_label, thread, flow._cancel_keyboard(state.nonce))
    task = asyncio.create_task(run(flow, adapter, chat, thread, key, state))
    flow.tasks[key] = task
    task.add_done_callback(lambda done: flow._release(key, done))
    return True


def validate_result(flow, state, result):
    if (result.get('operacion') != OPERATION or result.get('slug') != state.slug
            or result.get('cuit') != state.cuit or result.get('id_contribuyente') != state.contributor_id
            or result.get('escrituras_externas') is not False or result.get('escrituras_confirmadas') != []):
        raise RuntimeError('DDJJ_RESULTADO_NO_COINCIDE')
    cases = result.get('periodos')
    if not isinstance(cases, list) or [c.get('periodo') for c in cases] != state.ddjj_periods:
        raise RuntimeError('DDJJ_PERIODOS_NO_COINCIDEN')
    for case in cases:
        if case.get('estado') not in ('completado', 'no_presentado', 'no_consultada', 'fallido'):
            raise RuntimeError('DDJJ_ESTADO_INVALIDO')
        if case['estado'] != 'completado' and not case.get('motivo_texto'):
            raise RuntimeError('DDJJ_MOTIVO_FALTANTE')
    expected = any(c['estado'] == 'completado' for c in cases)
    record = result.get('xlsx')
    if not expected:
        if record is not None:
            raise RuntimeError('DDJJ_ARCHIVO_INESPERADO')
        return cases, None
    if not isinstance(record, dict):
        raise RuntimeError('DDJJ_ARCHIVO_FALTANTE')
    path = flow._saved(record.get('ruta'), record.get('ruta_clientes'), record.get('sha256'))
    if path.suffix.lower() != '.xlsx' or path.stat().st_size > 45_000_000:
        raise RuntimeError('DDJJ_ARCHIVO_INVALIDO')
    try:
        with ZipFile(path) as book:
            names = book.namelist()
            if len(names) != len(set(names)) or 'xl/workbook.xml' not in names:
                raise ValueError()
            info = book.getinfo('xl/workbook.xml')
            if info.file_size > 100_000:
                raise ValueError()
            xml = ElementTree.fromstring(book.read(info))
            ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
            if [s.attrib['name'] for s in xml.findall(f'{ns}sheets/{ns}sheet')] != ['IVA', 'Observaciones']:
                raise ValueError()
    except (BadZipFile, ValueError, KeyError, ElementTree.ParseError):
        raise RuntimeError('DDJJ_ARCHIVO_INVALIDO') from None
    return cases, path


async def checked_subject(flow, state):
    await asyncio.to_thread(flow._scope().require_permission, state.user_id, 'operaciones.ejecutar')
    verified = await asyncio.to_thread(flow._by_id, state.contributor_id, state.user_id)
    binding = state.scope_item or {}
    fields = ('id', 'slug', 'cuit', 'study_id', 'relation_id', 'relation_revision', 'access_id', 'representative_id')
    if len(verified) != 1 or not verified[0].get('access_id') or any(verified[0].get(k) != binding.get(k) for k in fields):
        raise RuntimeError('DDJJ_SELECCION_VENCIDA')
    return verified[0]


def acquire_locks(flow, state, key, locks):
    for period in state.ddjj_periods:
        lock = f'{state.contributor_id}:{period}'
        if lock in flow.execution_locks:
            raise RuntimeError('DDJJ_CONSULTA_EN_CURSO')
        flow._acquire_execution_lock(lock)
        locks.append(lock)
        flow.execution_locks[lock] = key


async def deliver(flow, adapter, chat, thread, state, staged):
    state.stage = 'delivering'
    if state.cancelled:
        raise asyncio.CancelledError()
    path = staged[0][0]
    delivery = await adapter.send_document(chat_id=str(chat), file_path=str(path), file_name=path.name,
        caption=f'{state.nombre} · DDJJ de IVA presentadas a Excel.', metadata={'thread_id': thread} if thread is not None else None)
    if not delivery.success or not getattr(delivery, 'message_id', None):
        raise RuntimeError('DDJJ_ENTREGA_NO_CONFIRMADA')


async def finish_cases(flow, state, cases, run_handle, items, finished, effects):
    lines = [f'{state.nombre} · DDJJ de IVA presentadas']
    for case in cases:
        period = case['periodo']; ok = case['estado'] == 'completado'
        text = 'Transcripta en el Excel.' if ok else case['motivo_texto']
        status = 'completado' if ok else 'rechazado' if case['estado'] == 'no_presentado' else 'fallido'
        await flow.history.finish_item(run_handle, items[period], state=status,
            reason_code='ddjj_transcripta' if ok else flow._history_reason_code(case.get('motivo')),
            reason_text=text, effects=effects, contributor_id=state.contributor_id, period=period, delivered_count=1 if ok else 0)
        finished.add(items[period]); lines.append(f'{period[5:]}/{period[:4]}: {text}')
    await flow.history.close(run_handle)
    return '\n'.join(lines)


async def run(flow, adapter, chat, thread, key, state):
    proc = None; staging = None; run_handle = None
    locks = []; items = {}; finished = set()
    effects = [{'codigo': 'consultar', 'realizado': True}, {'codigo': 'preparar', 'realizado': False},
               {'codigo': 'presentar', 'realizado': False}, {'codigo': 'pagar', 'realizado': False}]
    consulted = False
    async def remaining(status, code, text):
        if run_handle is None:
            return
        for period, item in items.items():
            if item not in finished:
                await flow.history.finish_item(run_handle, item, state=status, reason_code=code,
                    reason_text=text, effects=[{**e, 'realizado': consulted if e['codigo']=='consultar' else False} for e in effects],
                    contributor_id=state.contributor_id, period=period, delivered_count=0)
                finished.add(item)
        await flow.history.close(run_handle)
    try:
        if flow.history is None:
            raise RuntimeError('DDJJ_HISTORIAL_NO_DISPONIBLE')
        run_handle = await flow.history.start(telegram_id=int(state.user_id), operation=OPERATION,
            key_material=f'ddjj:{chat}:{thread}:{state.nonce}', reference=f'{state.slug}-{state.ddjj_periods[0]}', lease_seconds=1920)
        prepared = await flow.history.prepare_items(run_handle, state.ddjj_periods)
        items = {p: int(i['id_item']) for p, i in zip(state.ddjj_periods, prepared, strict=True)}
        verified = await checked_subject(flow, state)
        acquire_locks(flow, state, key, locks)
        if state.cancelled:
            raise asyncio.CancelledError()
        proc = await asyncio.create_subprocess_exec(*command(flow, state), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=256*1024)
        flow.processes[key] = proc
        consulted = True
        raw, _ = await asyncio.wait_for(flow._communicate_with_progress(proc, state, adapter=adapter,
            chat_id=chat, thread_id=thread), timeout=1800)
        result = flow._parse_result(raw)
        if proc.returncode != 0 or result.get('ok') is not True:
            raise RuntimeError('DDJJ_EJECUCION_NO_COMPLETADA')
        cases, source = validate_result(flow, state, result)
        await verify_representation(state.user_id, verified, 'ARCA', str(result.get('representado_verificado', '')))
        if source is not None:
            await flow.history.attach_run(run_handle, contributor_id=state.contributor_id, output=source,
                output_relative=str(source.relative_to(flow.clients_root)), libro='ddjj_iva')
            staging, staged = flow._stage_delivery_files([(source, len(cases), 'DDJJ de IVA')])
            await deliver(flow, adapter, chat, thread, state, staged)
        text = await finish_cases(flow, state, cases, run_handle, items, finished, effects)
        await flow._edit_progress(state, text)
        await flow._send(adapter, chat, text, thread)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        await asyncio.shield(remaining('cancelado', 'cancelado_por_usuario', 'Consulta cancelada por el usuario.'))
        raise
    except (RuntimeError, ValueError, TypeError, OSError, TelegramError) as exc:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        code = str(exc) if re.fullmatch('[A-Z0-9_]{1,100}', str(exc)) else 'DDJJ_ERROR_TECNICO'
        text = ('No se pudo completar DDJJ de IVA a Excel. No se modificó ARCA. '
                'Revisá el resultado en Operaciones; el diagnóstico quedó preservado.')
        await remaining('fallido', code.lower(), text)
        await flow._edit_progress(state, text)
        await flow._send(adapter, chat, text, thread)
    finally:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        flow.processes.pop(key, None)
        for lock in locks:
            flow.execution_locks.pop(lock, None); flow._release_execution_lock(lock)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
