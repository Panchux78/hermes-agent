"""ARCA › Mis Retenciones (#157, C-071): contrato propio sobre el transporte y el selector de Portal IVA.

Organismos fiscales › ARCA › Consultar › Mis Retenciones › contribuyente › impuesto › tipo ›
mes o rango. Mismo ejecutor que el panel (``mis_retenciones.py``), mismo CAPTCHA por Telegram,
misma bitácora. Sólo lectura.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from plugins.platforms.telegram.fiscal_credentials import verify_representation
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.period_input import SPECS, parse_period, propose_period, sender

OPERATION = 'arca_mis_retenciones'
logger = logging.getLogger(__name__)
# Mismo orden que el panel. Ganancias son dos códigos de ARCA (217 y 787) desde el 14/11/2026.
IMPUESTOS = {'iva': ('IVA', ('767', '216')), 'ganancias': ('Ganancias', ('217', '787')),
             'bienes_personales': ('Bienes Personales', ('219',)), 'suss': ('Seguridad social', ('353',))}
# No estar inscripto en IVA no impide consultar lo demás: sólo la opción IVA responde «no corresponde».
IVA_NO_CORRESPONDE = {
    'monotributo': 'IVA: no corresponde. ARCA lo registra como monotributista, sin inscripción en IVA.',
    'sin_inscripcion_verificada': 'IVA: no corresponde. La ficha fiscal no tiene una inscripción en IVA verificada en ARCA.',
}
OTROS_IMPUESTOS = 'Podés consultar Ganancias, Bienes Personales o Seguridad social.'
TIPOS = {'ambas': 'Retenciones y percepciones', 'retencion': 'Sólo retenciones', 'percepcion': 'Sólo percepciones'}
PROMPT = SPECS['mis-retenciones'].prompt
ESTADOS = {'completado': 'obtenido', 'cero': 'sin movimientos', 'no_aplica': 'no corresponde', 'fallido': 'no obtenido',
           'formato': 'original guardado, sin Excel', 'control': 'no coincide con la pantalla', 'incompleto': 'incompleto'}
ETAPAS = {'filtro': 'Completando el filtro', 'consulta': 'Consultando', 'exportacion': 'Esperando el archivo de ARCA',
          'control': 'Controlando el archivo contra la pantalla'}
GENERALES = {'ingreso': 'Ingresando a ARCA…', 'servicio': 'Abriendo Mis Retenciones…',
             'representado': 'Seleccionando el contribuyente…', 'excel': 'Generando el Excel…'}


def impuesto_keyboard(nonce):
    rows = [[InlineKeyboardButton(nombre, callback_data=f'pi:mri:{nonce}:{clave}')] for clave, (nombre, _) in IMPUESTOS.items()]
    rows.append([InlineKeyboardButton(menu_label('❌', 'Cancelar'), callback_data=f'pi:cancel:{nonce}')])
    return InlineKeyboardMarkup(rows)


def tipo_keyboard(nonce):
    rows = [[InlineKeyboardButton(texto, callback_data=f'pi:mrt:{nonce}:{clave}')] for clave, texto in TIPOS.items()]
    rows.append([InlineKeyboardButton(menu_label('❌', 'Cancelar'), callback_data=f'pi:cancel:{nonce}')])
    return InlineKeyboardMarkup(rows)


def periodo(texto):
    """Adaptador al mismo parser que el resto del bot; conserva fechas exactas."""
    return parse_period(texto, 'mis-retenciones').dates()


def progress(stage):
    m = re.fullmatch(r'mr:(?:([0-9]{3}):)?([a-z]+)', stage)
    if not m:
        return None
    if m[1]:
        return f'{m[1]} · {ETAPAS[m[2]]}…' if m[2] in ETAPAS else None
    return GENERALES.get(m[2])


async def after_contributor(flow, adapter, chat, thread, state):
    state.stage = 'mr_impuesto'
    await flow._send(adapter, chat, f'Contribuyente: {state.nombre}.\nElegí el impuesto.', thread, impuesto_keyboard(state.nonce))


async def callback(flow, adapter, query, data, chat, thread, key):
    m = re.fullmatch(r'pi:(mri|mrt):([0-9a-f]{10}):([a-z_]+)', data)
    if not m:
        return False
    state = flow.states.get(key)
    esperado = 'mr_impuesto' if m[1] == 'mri' else 'mr_tipo'
    if not state or state.nonce != m[2] or state.stage != esperado:
        await query.answer('Esta opción venció. Iniciá Mis Retenciones nuevamente.')
        return True
    if m[1] == 'mri':
        if m[3] not in IMPUESTOS:
            await query.answer('Impuesto inválido.')
            return True
        motivo = IVA_NO_CORRESPONDE.get((getattr(state, 'scope_item', None) or {}).get('iva')) if m[3] == 'iva' else None
        if motivo:
            # No se abre ARCA ni se registra una consulta: la persona elige otro impuesto.
            await query.answer('IVA: no corresponde.')
            await flow._send(adapter, chat, f'{motivo}\n{OTROS_IMPUESTOS}', thread, impuesto_keyboard(state.nonce))
            return True
        state.mr_impuesto = m[3]
        await query.answer(IMPUESTOS[m[3]][0])
        if m[3] == 'suss':
            state.mr_tipo = 'retencion'
            state.stage = 'mr_period'
            await flow._send(adapter, chat, 'Seguridad social se consulta como retenciones y trae también el archivo para '
                             'aplicativo tal como lo exporta ARCA.\n' + PROMPT, thread, flow._cancel_keyboard(state.nonce))
        else:
            state.stage = 'mr_tipo'
            await flow._send(adapter, chat, 'Elegí el tipo de operación.', thread, tipo_keyboard(state.nonce))
        return True
    if m[3] not in TIPOS:
        await query.answer('Tipo inválido.')
        return True
    state.mr_tipo = m[3]
    state.stage = 'mr_period'
    await query.answer(TIPOS[m[3]])
    await flow._send(adapter, chat, PROMPT, thread, flow._cancel_keyboard(state.nonce))
    return True


async def receive_period(flow, adapter, message, state, key):
    chat, thread = message.chat_id, getattr(message, 'message_thread_id', None)
    await propose_period(state, 'mis-retenciones', message.text, prefix='pi',
        send=sender(adapter, chat, thread),
        submit=lambda selection: submit_period(flow, adapter, message, state, key, selection))
    return True


async def submit_period(flow, adapter, message, state, key, selection):
    chat, thread = message.chat_id, getattr(message, 'message_thread_id', None)
    state.mr_desde, state.mr_hasta = selection.dates()
    state.period = f'{state.mr_hasta[6:]}-{state.mr_hasta[3:5]}'
    state.stage = 'running'
    nombre = IMPUESTOS[state.mr_impuesto][0]
    state.progress_label = f'{state.nombre} · Mis Retenciones {nombre} · {state.mr_desde} a {state.mr_hasta}…'
    state.progress_message = await flow._send(adapter, chat, state.progress_label, thread, flow._cancel_keyboard(state.nonce))
    task = asyncio.create_task(run(flow, adapter, chat, thread, key, state))
    flow.tasks[key] = task
    task.add_done_callback(lambda done: flow._release(key, done))
    return True


def command(flow, state):
    executor = flow.executor.with_name('mis_retenciones.py')
    if not flow.available() or not executor.is_file():
        raise RuntimeError('MR_EJECUTOR_NO_DISPONIBLE')
    binding = state.scope_item or {}
    if not binding.get('access_id') or not binding.get('representative_id'):
        raise RuntimeError('MR_ACCESO_NO_DISPONIBLE')
    return [str(flow.uv), 'run', '--no-project', '--with', 'selenium', '--with', 'openpyxl',
            'xvfb-run', '-a', 'python3', str(executor), '--cliente', state.slug,
            '--id-acceso', str(int(binding['access_id'])), '--id-titular', str(int(binding['representative_id'])),
            '--impuesto', state.mr_impuesto, '--tipo', state.mr_tipo, '--desde', state.mr_desde,
            '--hasta', state.mr_hasta, '--captcha-stdin']


def validate_result(flow, state, result):
    if result.get('ok') is not True:
        code = str(result.get('motivo') or '')
        raise RuntimeError(code if re.fullmatch(r'[A-Z0-9_]{3,100}', code) else 'MR_EJECUCION_NO_COMPLETADA')
    if (result.get('operacion') != OPERATION or result.get('slug') != state.slug or result.get('cuit') != state.cuit
            or result.get('id_contribuyente') != state.contributor_id or result.get('escrituras_externas') is not False
            or result.get('escrituras_confirmadas') != [] or result.get('impuesto') != state.mr_impuesto
            or result.get('tipo') != state.mr_tipo or result.get('desde') != state.mr_desde
            or result.get('hasta') != state.mr_hasta):
        raise RuntimeError('MR_RESULTADO_NO_COINCIDE')
    codigos = list(IMPUESTOS[state.mr_impuesto][1])
    consultas = result.get('consultas')
    if not isinstance(consultas, list) or not consultas or [c.get('codigo') for c in consultas] != codigos[:len(consultas)]:
        raise RuntimeError('MR_CONSULTAS_NO_COINCIDEN')
    archivos = []
    for c in consultas:
        if c.get('estado') not in ESTADOS or (c['estado'] != 'completado' and not c.get('motivo_texto')):
            raise RuntimeError('MR_ESTADO_INVALIDO')
        if c.get('archivo'):
            path = flow._saved(c['archivo'].get('ruta'), c['archivo'].get('ruta_clientes'), c['archivo'].get('sha256'))
            archivos.append((path, f"{state.nombre} · Mis Retenciones {c['codigo']} · original de ARCA"))
        if c.get('aplicativo'):
            path = flow._saved(c['aplicativo'].get('ruta'), c['aplicativo'].get('ruta_clientes'), c['aplicativo'].get('sha256'))
            archivos.append((path, f"{state.nombre} · Mis Retenciones {c['codigo']} · para aplicativo (SIAP)"))
    excel = None
    if result.get('xlsx'):
        record = result['xlsx']
        excel = flow._saved(record.get('ruta'), record.get('ruta_clientes'), record.get('sha256'))
        if excel.suffix.lower() != '.xlsx':
            raise RuntimeError('MR_ARCHIVO_INVALIDO')
    if bool(excel) != any(c['estado'] in ('completado', 'incompleto') for c in consultas):
        raise RuntimeError('MR_EXCEL_NO_COINCIDE')
    return consultas, excel, archivos


async def checked_subject(flow, state):
    await asyncio.to_thread(flow._scope().require_permission, state.user_id, 'operaciones.ejecutar')
    verified = await asyncio.to_thread(flow._by_id, state.contributor_id, state.user_id, 'mis-retenciones')
    binding = state.scope_item or {}
    fields = ('id', 'slug', 'cuit', 'study_id', 'relation_id', 'relation_revision', 'access_id', 'representative_id')
    if len(verified) != 1 or not verified[0].get('access_id') or any(verified[0].get(k) != binding.get(k) for k in fields):
        raise RuntimeError('MR_SELECCION_VENCIDA')
    if state.mr_impuesto == 'iva' and verified[0].get('iva') in IVA_NO_CORRESPONDE:
        raise RuntimeError('MR_IVA_NO_CORRESPONDE')
    return verified[0]


def linea(c):
    estado = ESTADOS[c['estado']]
    if c['estado'] == 'completado':
        total = f"{float(c['total_pantalla']):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
        return f"{c['etiqueta']}: {c['cantidad_pantalla']} registros, total $ {total} (igual que la pantalla de ARCA)."
    if c['estado'] == 'cero':
        return f"{c['etiqueta']}: ARCA informó que no hay resultados para este filtro."
    return f"{c['etiqueta']}: {estado}. {c.get('motivo_texto', '')}".strip()


async def run(flow, adapter, chat, thread, key, state):
    proc = None; staging = None; run_handle = None
    lock = f'{state.contributor_id}:mis-retenciones'; locked = False
    items = {}; finished = set()
    effects = [{'codigo': 'consultar', 'realizado': True}, {'codigo': 'preparar', 'realizado': False},
               {'codigo': 'presentar', 'realizado': False}, {'codigo': 'pagar', 'realizado': False}]
    consulted = False
    mensaje = None
    codigos = list(IMPUESTOS[state.mr_impuesto][1])

    async def remaining(status, code, text):
        if run_handle is None:
            return
        for codigo, item in items.items():
            if item not in finished:
                await flow.history.finish_item(run_handle, item, state=status, reason_code=code, reason_text=text,
                    effects=[{**e, 'realizado': consulted if e['codigo'] == 'consultar' else False} for e in effects],
                    contributor_id=state.contributor_id, period=state.period, delivered_count=0)
                finished.add(item)
        await flow.history.close(run_handle)
    try:
        if flow.history is None:
            raise RuntimeError('MR_HISTORIAL_NO_DISPONIBLE')
        run_handle = await flow.history.start(telegram_id=int(state.user_id), operation=OPERATION,
            key_material=f'mr:{chat}:{thread}:{state.nonce}', reference=f'{state.slug}-{state.mr_impuesto}-{state.period}',
            lease_seconds=1920)
        refs = [f'Mis Retenciones {c} · {state.mr_desde} a {state.mr_hasta}' for c in codigos]
        prepared = await flow.history.prepare_items(run_handle, refs)
        items = {c: int(i['id_item']) for c, i in zip(codigos, prepared, strict=True)}
        verified = await checked_subject(flow, state)
        if lock in flow.execution_locks:
            raise RuntimeError('MR_CONSULTA_EN_CURSO')
        flow._acquire_execution_lock(lock); locked = True
        flow.execution_locks[lock] = key
        if state.cancelled:
            raise asyncio.CancelledError()
        proc = await asyncio.create_subprocess_exec(*command(flow, state), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=256*1024)
        flow.processes[key] = proc
        consulted = True
        raw, _ = await asyncio.wait_for(flow._communicate_with_progress(proc, state, adapter=adapter,
            chat_id=chat, thread_id=thread), timeout=1800)
        result = flow._parse_result(raw)
        if result.get('ok') is not True and isinstance(result.get('mensaje'), str) and len(result['mensaje']) < 400:
            mensaje = result['mensaje']
        consultas, excel, archivos = validate_result(flow, state, result)
        await verify_representation(state.user_id, verified, 'ARCA', str(result.get('representado_verificado', '')))
        rels = [*(p for p, _ in archivos), *([excel] if excel else [])]
        for path in rels:
            await flow.history.attach_run(run_handle, contributor_id=state.contributor_id, output=path,
                output_relative=str(path.relative_to(flow.clients_root)), libro='mis_retenciones')
        state.stage = 'delivering'
        envios = ([(excel, f'{state.nombre} · Mis Retenciones {IMPUESTOS[state.mr_impuesto][0]} · Excel')] if excel else []) + archivos
        if envios:
            staging, staged = flow._stage_delivery_files([(p, 0, caption) for p, caption in envios])
            for path, _, caption in staged:
                delivery = await adapter.send_document(chat_id=str(chat), file_path=str(path), file_name=path.name,
                    caption=caption, metadata={'thread_id': thread} if thread is not None else None)
                if not delivery.success or not getattr(delivery, 'message_id', None):
                    raise RuntimeError('MR_ENTREGA_NO_CONFIRMADA')
                entregado = getattr(delivery, 'delivered_filename', None)
                if entregado is not None and entregado != path.name:
                    # El nombre tiene que ser el mismo que en el panel; si Telegram lo cambia, queda a la vista.
                    logger.warning('[MIS-RETENCIONES] Telegram cambió el nombre del archivo (%d → %d caracteres)',
                                   len(path.name), len(entregado))
        for c in consultas:
            ok = c['estado'] in ('completado', 'cero')
            await flow.history.finish_item(run_handle, items[c['codigo']],
                state='completado' if ok else 'rechazado' if c['estado'] == 'no_aplica' else 'fallido',
                reason_code=f"mis_retenciones_{c['estado']}", reason_text=linea(c), effects=effects,
                contributor_id=state.contributor_id, period=state.period,
                delivered_count=c.get('cantidad_pantalla', 0) if c['estado'] == 'completado' else 0)
            finished.add(items[c['codigo']])
        await remaining('fallido', 'mis_retenciones_no_consultada',
                        'No se consultó: la identidad del contribuyente quedó en duda en el impuesto anterior.')
        text = '\n'.join([f'{state.nombre} · Mis Retenciones {IMPUESTOS[state.mr_impuesto][0]} · {state.mr_desde} a {state.mr_hasta}',
                          *(linea(c) for c in consultas)] + ([] if envios else ['No se descargó ningún archivo.']))
        # El resumen va una sola vez (14/11/2026): el mensaje de avance se cierra con una línea
        # corta y el detalle llega en un único mensaje, después de los archivos.
        await flow._edit_progress(state, f'{state.nombre} · Mis Retenciones {IMPUESTOS[state.mr_impuesto][0]}: consulta terminada.')
        await flow._send(adapter, chat, text, thread)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        await asyncio.shield(remaining('cancelado', 'cancelado_por_usuario', 'Consulta cancelada por el usuario.'))
        raise
    except (RuntimeError, ValueError, TypeError, OSError, TelegramError) as exc:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        code = str(exc) if re.fullmatch('[A-Z0-9_]{1,100}', str(exc)) else 'MR_ERROR_TECNICO'
        en_curso = code in ('MR_CONSULTA_EN_CURSO', 'PORTAL_IVA_EXECUTION_ALREADY_RUNNING')
        detalle = ('Ya hay una consulta de Mis Retenciones en curso para este contribuyente. No se inició otra en ARCA.' if en_curso
                   else f"{IVA_NO_CORRESPONDE['sin_inscripcion_verificada']} No se consultó ARCA. {OTROS_IMPUESTOS}" if code == 'MR_IVA_NO_CORRESPONDE'
                   else f'No se pudo completar Mis Retenciones. {mensaje} No se modificó ARCA.' if mensaje
                   else 'No se pudo completar Mis Retenciones. No se modificó ARCA. '
                        'Revisá el resultado en Operaciones; el diagnóstico quedó preservado.')
        await remaining('rechazado' if en_curso else 'fallido', code.lower(), detalle)
        await flow._edit_progress(state, 'Mis Retenciones: la consulta no se completó.')
        await flow._send(adapter, chat, detalle, thread)
    finally:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        flow.processes.pop(key, None)
        if locked:
            flow.execution_locks.pop(lock, None); flow._release_execution_lock(lock)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
