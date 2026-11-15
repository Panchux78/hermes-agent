"""ARCA › Mis Comprobantes (#158, C-066/C-067): contrato propio sobre el transporte y el selector de Portal IVA.

Organismos fiscales › ARCA › Consultar › Mis Comprobantes › contribuyente › Emitidos / Recibidos /
Emitidos y recibidos › mes, año o rango de fechas. Mismo ejecutor que el panel
(``mis_comprobantes.py``), mismo CAPTCHA por Telegram, misma bitácora. Sólo lectura.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from plugins.platforms.telegram.fiscal_credentials import verify_representation
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.period_input import SPECS, propose_period, sender

OPERATION = 'arca_mis_comprobantes'
logger = logging.getLogger(__name__)
# Una sola función con tres opciones (decisión de Pancho del 15/11/2026). Mismo orden que el panel.
SENTIDOS = {'emitidos': ('Emitidos', ('emitidos',)), 'recibidos': ('Recibidos', ('recibidos',)),
            'ambos': ('Emitidos y recibidos', ('emitidos', 'recibidos'))}
ETIQUETA = {'emitidos': 'Emitidos', 'recibidos': 'Recibidos'}
TOPE_DIAS = 365  # El selector de fechas de ARCA admite hasta = desde + 365 días (observado el 15/11/2026).
# Orden de fuentes del estudio: para compras manda el Libro IVA presentado en Portal IVA.
AVISO_RECIBIDOS = ('Para las compras, la fuente contable es el Libro IVA presentado en Portal IVA. Recibidos de Mis '
                   'Comprobantes es una consulta de verificación o de respaldo y no discrimina percepciones por régimen.')
PROMPT = SPECS['mis-comprobantes'].prompt + '\nHasta 365 días por consulta, que es el tope de ARCA.'
ESTADOS = {'completado': 'obtenido', 'cero': 'sin comprobantes', 'fallido': 'no obtenido',
           'formato': 'original guardado, sin Excel', 'control': 'no coincide con la pantalla'}
ETAPAS = {'filtro': 'Poniendo el rango de fechas', 'consulta': 'Consultando', 'descarga': 'Esperando el archivo de ARCA',
          'control': 'Controlando el archivo contra la pantalla'}
GENERALES = {'ingreso': 'Ingresando a ARCA…', 'servicio': 'Abriendo Mis Comprobantes…',
             'representado': 'Seleccionando el contribuyente…', 'excel': 'Generando el Excel…'}


def sentido_keyboard(nonce):
    rows = [[InlineKeyboardButton(nombre, callback_data=f'pi:mcs:{nonce}:{clave}')] for clave, (nombre, _) in SENTIDOS.items()]
    rows.append([InlineKeyboardButton(menu_label('❌', 'Cancelar'), callback_data=f'pi:cancel:{nonce}')])
    return InlineKeyboardMarkup(rows)


def progress(stage):
    m = re.fullmatch(r'mc:(?:(emitidos|recibidos):)?([a-z]+)', stage)
    if not m:
        return None
    if m[1]:
        return f'{ETIQUETA[m[1]]} · {ETAPAS[m[2]]}…' if m[2] in ETAPAS else None
    return GENERALES.get(m[2])


def rango_efectivo(selection, today=None):
    """Días exactos pedidos. Un mes o un año en curso llegan hasta hoy: ARCA no acepta un rango que termine en el futuro."""
    today = today or datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date()
    desde, hasta = (datetime.strptime(v, '%d/%m/%Y').date() for v in selection.dates())
    if desde > today:
        raise ValueError('Ese período todavía no empezó.')
    if hasta > today:
        hasta = today
    if hasta > desde + timedelta(days=TOPE_DIAS):
        raise ValueError('ARCA admite hasta 365 días por consulta. Pedí un rango más corto.')
    return desde.strftime('%d/%m/%Y'), hasta.strftime('%d/%m/%Y')


async def after_contributor(flow, adapter, chat, thread, state):
    state.stage = 'mc_sentido'
    await flow._send(adapter, chat, f'Contribuyente: {state.nombre}.\nElegí qué comprobantes consultar.', thread,
                     sentido_keyboard(state.nonce))


async def callback(flow, adapter, query, data, chat, thread, key):
    m = re.fullmatch(r'pi:mcs:([0-9a-f]{10}):([a-z]+)', data)
    if not m:
        return False
    state = flow.states.get(key)
    if not state or state.nonce != m[1] or state.stage != 'mc_sentido':
        await query.answer('Esta opción venció. Iniciá Mis Comprobantes nuevamente.')
        return True
    if m[2] not in SENTIDOS:
        await query.answer('Opción inválida.')
        return True
    state.mc_sentido = m[2]
    state.stage = 'mc_period'
    await query.answer(SENTIDOS[m[2]][0])
    aviso = '' if m[2] == 'emitidos' else AVISO_RECIBIDOS + '\n'
    await flow._send(adapter, chat, aviso + PROMPT, thread, flow._cancel_keyboard(state.nonce))
    return True


async def receive_period(flow, adapter, message, state, key):
    chat, thread = message.chat_id, getattr(message, 'message_thread_id', None)
    await propose_period(state, 'mis-comprobantes', message.text, prefix='pi',
        send=sender(adapter, chat, thread),
        submit=lambda selection: submit_period(flow, adapter, message, state, key, selection))
    return True


async def submit_period(flow, adapter, message, state, key, selection):
    chat, thread = message.chat_id, getattr(message, 'message_thread_id', None)
    try:
        state.mc_desde, state.mc_hasta = rango_efectivo(selection)
    except ValueError as exc:
        # El período confirmado no se puede consultar: se dice por qué y se pide otro, sin abrir ARCA.
        state.stage = 'mc_period'
        await flow._send(adapter, chat, f'{exc}\n{PROMPT}', thread, flow._cancel_keyboard(state.nonce))
        return True
    state.period = f'{state.mc_hasta[6:]}-{state.mc_hasta[3:5]}'
    state.stage = 'running'
    state.progress_label = f'{state.nombre} · Mis Comprobantes {SENTIDOS[state.mc_sentido][0]} · {state.mc_desde} a {state.mc_hasta}…'
    state.progress_message = await flow._send(adapter, chat, state.progress_label, thread, flow._cancel_keyboard(state.nonce))
    task = asyncio.create_task(run(flow, adapter, chat, thread, key, state))
    flow.tasks[key] = task
    task.add_done_callback(lambda done: flow._release(key, done))
    return True


def libros_iva(flow, state):
    """Estado del Libro IVA Compras de cada mes del rango; sólo cuando se piden Recibidos."""
    if state.mc_sentido == 'emitidos':
        return []
    iso = lambda v: f'{v[6:]}-{v[3:5]}-{v[:2]}'
    rows = flow._query('SELECT console.fn_mis_comprobantes_libros_iva_telegram('
                       f"{int(state.user_id)},{int(state.contributor_id)},'{iso(state.mc_desde)}'::date,'{iso(state.mc_hasta)}'::date)::text;")
    value = json.loads(rows[0]) if rows and rows[0] else None
    if not (isinstance(value, list) and all(isinstance(x, list) and len(x) == 2 and re.fullmatch(r'\d{4}-\d{2}', str(x[0]))
                                            and isinstance(x[1], str) and len(x[1]) <= 120 for x in value)):
        raise RuntimeError('MC_LIBROS_IVA_NO_DISPONIBLE')
    return value


def command(flow, state, libros):
    executor = flow.executor.with_name('mis_comprobantes.py')
    if not flow.available() or not executor.is_file():
        raise RuntimeError('MC_EJECUTOR_NO_DISPONIBLE')
    binding = state.scope_item or {}
    if not binding.get('access_id') or not binding.get('representative_id'):
        raise RuntimeError('MC_ACCESO_NO_DISPONIBLE')
    return [str(flow.uv), 'run', '--no-project', '--with', 'selenium', '--with', 'openpyxl',
            'xvfb-run', '-a', 'python3', str(executor), '--cliente', state.slug,
            '--id-acceso', str(int(binding['access_id'])), '--id-titular', str(int(binding['representative_id'])),
            '--sentido', state.mc_sentido, '--desde', state.mc_desde, '--hasta', state.mc_hasta,
            '--libros-iva', json.dumps(libros, ensure_ascii=False), '--captcha-stdin']


def validate_result(flow, state, result):
    if result.get('ok') is not True:
        code = str(result.get('motivo') or '')
        raise RuntimeError(code if re.fullmatch(r'[A-Z0-9_]{3,100}', code) else 'MC_EJECUCION_NO_COMPLETADA')
    if (result.get('operacion') != OPERATION or result.get('slug') != state.slug or result.get('cuit') != state.cuit
            or result.get('id_contribuyente') != state.contributor_id or result.get('escrituras_externas') is not False
            or result.get('escrituras_confirmadas') != [] or result.get('sentido') != state.mc_sentido
            or result.get('desde') != state.mc_desde or result.get('hasta') != state.mc_hasta):
        raise RuntimeError('MC_RESULTADO_NO_COINCIDE')
    sentidos = list(SENTIDOS[state.mc_sentido][1])
    consultas = result.get('consultas')
    if not isinstance(consultas, list) or not consultas or [c.get('sentido') for c in consultas] != sentidos[:len(consultas)]:
        raise RuntimeError('MC_CONSULTAS_NO_COINCIDEN')
    archivos = []
    for c in consultas:
        if c.get('estado') not in ESTADOS or (c['estado'] != 'completado' and not c.get('motivo_texto')):
            raise RuntimeError('MC_ESTADO_INVALIDO')
        if c['estado'] in ('completado', 'cero') and (type(c.get('cantidad_pantalla')) is not int or c['cantidad_pantalla'] < 0
                                                     or (c['estado'] == 'cero') != (c['cantidad_pantalla'] == 0)):
            raise RuntimeError('MC_CANTIDAD_INVALIDA')
        if c.get('archivo'):
            path = flow._saved(c['archivo'].get('ruta'), c['archivo'].get('ruta_clientes'), c['archivo'].get('sha256'))
            if path.suffix.lower() != '.zip':
                raise RuntimeError('MC_ARCHIVO_INVALIDO')
            archivos.append((path, f"{state.nombre} · Mis Comprobantes {ETIQUETA[c['sentido']]} · original de ARCA"))
        elif c['estado'] in ('completado', 'formato', 'control'):
            raise RuntimeError('MC_ARCHIVO_FALTANTE')
    excel = None
    if result.get('xlsx'):
        record = result['xlsx']
        excel = flow._saved(record.get('ruta'), record.get('ruta_clientes'), record.get('sha256'))
        if excel.suffix.lower() != '.xlsx':
            raise RuntimeError('MC_ARCHIVO_INVALIDO')
    sin_excel = result.get('excel_motivo_texto') if result.get('excel_motivo') else None
    if bool(excel) != (any(c['estado'] == 'completado' for c in consultas) and not result.get('excel_motivo')) \
            or (result.get('excel_motivo') and not isinstance(sin_excel, str)):
        raise RuntimeError('MC_EXCEL_NO_COINCIDE')
    return consultas, excel, archivos, sin_excel


async def checked_subject(flow, state):
    await asyncio.to_thread(flow._scope().require_permission, state.user_id, 'operaciones.ejecutar')
    verified = await asyncio.to_thread(flow._by_id, state.contributor_id, state.user_id, 'mis-comprobantes')
    binding = state.scope_item or {}
    fields = ('id', 'slug', 'cuit', 'study_id', 'relation_id', 'relation_revision', 'access_id', 'representative_id')
    if len(verified) != 1 or not verified[0].get('access_id') or any(verified[0].get(k) != binding.get(k) for k in fields):
        raise RuntimeError('MC_SELECCION_VENCIDA')
    return verified[0]


def linea(c):
    if c['estado'] == 'completado':
        cantidad = f"{c['cantidad_pantalla']:,}".replace(',', '.')
        return f"{c['etiqueta']}: {cantidad} comprobantes (igual que la pantalla de ARCA)."
    if c['estado'] == 'cero':
        return f"{c['etiqueta']}: ARCA informó que no hay comprobantes para este filtro."
    return f"{c['etiqueta']}: {ESTADOS[c['estado']]}. {c.get('motivo_texto', '')}".strip()


def fuente_compras(libros):
    """Qué es Recibidos y, por mes, si el Libro IVA oficial ya está en Documentos."""
    return [AVISO_RECIBIDOS, *(f'Libro IVA Compras {m[5:]}/{m[:4]}: {estado}' for m, estado in libros)]


async def run(flow, adapter, chat, thread, key, state):
    proc = None; staging = None; run_handle = None
    lock = f'{state.contributor_id}:mis-comprobantes'; locked = False
    items = {}; finished = set()
    effects = [{'codigo': 'consultar', 'realizado': True}, {'codigo': 'preparar', 'realizado': False},
               {'codigo': 'presentar', 'realizado': False}, {'codigo': 'pagar', 'realizado': False}]
    consulted = False
    mensaje = None
    nombre = SENTIDOS[state.mc_sentido][0]
    sentidos = list(SENTIDOS[state.mc_sentido][1])

    async def remaining(status, code, text):
        if run_handle is None:
            return
        for item in items.values():
            if item not in finished:
                await flow.history.finish_item(run_handle, item, state=status, reason_code=code, reason_text=text,
                    effects=[{**e, 'realizado': consulted if e['codigo'] == 'consultar' else False} for e in effects],
                    contributor_id=state.contributor_id, period=state.period, delivered_count=0)
                finished.add(item)
        await flow.history.close(run_handle)
    try:
        if flow.history is None:
            raise RuntimeError('MC_HISTORIAL_NO_DISPONIBLE')
        run_handle = await flow.history.start(telegram_id=int(state.user_id), operation=OPERATION,
            key_material=f'mc:{chat}:{thread}:{state.nonce}', reference=f'{state.slug}-{state.mc_sentido}-{state.period}',
            lease_seconds=1920)
        refs = [f'Mis Comprobantes {ETIQUETA[s]} · {state.mc_desde} a {state.mc_hasta}' for s in sentidos]
        prepared = await flow.history.prepare_items(run_handle, refs)
        items = {s: int(i['id_item']) for s, i in zip(sentidos, prepared, strict=True)}
        verified = await checked_subject(flow, state)
        libros = await asyncio.to_thread(libros_iva, flow, state)
        if lock in flow.execution_locks:
            raise RuntimeError('MC_CONSULTA_EN_CURSO')
        flow._acquire_execution_lock(lock); locked = True
        flow.execution_locks[lock] = key
        if state.cancelled:
            raise asyncio.CancelledError()
        proc = await asyncio.create_subprocess_exec(*command(flow, state, libros), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=256*1024)
        flow.processes[key] = proc
        consulted = True
        raw, _ = await asyncio.wait_for(flow._communicate_with_progress(proc, state, adapter=adapter,
            chat_id=chat, thread_id=thread), timeout=1800)
        result = flow._parse_result(raw)
        if result.get('ok') is not True and isinstance(result.get('mensaje'), str) and len(result['mensaje']) < 400:
            mensaje = result['mensaje']
        consultas, excel, archivos, sin_excel = validate_result(flow, state, result)
        await verify_representation(state.user_id, verified, 'ARCA', str(result.get('representado_verificado', '')))
        rels = [*(p for p, _ in archivos), *([excel] if excel else [])]
        for path in rels:
            await flow.history.attach_run(run_handle, contributor_id=state.contributor_id, output=path,
                output_relative=str(path.relative_to(flow.clients_root)), libro='mis_comprobantes')
        state.stage = 'delivering'
        envios = ([(excel, f'{state.nombre} · Mis Comprobantes {nombre} · Excel')] if excel else []) + archivos
        no_enviados = []
        if envios:
            staging, staged = flow._stage_delivery_files([(p, 0, caption) for p, caption in envios])
            for path, _, caption in staged:
                # Un archivo ya obtenido no se pierde ni se vuelve a consultar por un fallo de envío:
                # queda en Documentos y se dice dónde está.
                try:
                    delivery = await adapter.send_document(chat_id=str(chat), file_path=str(path), file_name=path.name,
                        caption=caption, metadata={'thread_id': thread} if thread is not None else None)
                    enviado = bool(delivery.success and getattr(delivery, 'message_id', None))
                except (TelegramError, OSError):
                    enviado = False
                if not enviado:
                    no_enviados.append(path.name)
                    continue
                entregado = getattr(delivery, 'delivered_filename', None)
                if entregado is not None and entregado != path.name:
                    logger.warning('[MIS-COMPROBANTES] Telegram cambió el nombre del archivo (%d → %d caracteres)',
                                   len(path.name), len(entregado))
        for c in consultas:
            ok = c['estado'] in ('completado', 'cero')
            await flow.history.finish_item(run_handle, items[c['sentido']], state='completado' if ok else 'fallido',
                reason_code=f"mis_comprobantes_{c['estado']}", reason_text=linea(c), effects=effects,
                contributor_id=state.contributor_id, period=state.period,
                delivered_count=c.get('cantidad_pantalla', 0) if c['estado'] == 'completado' else 0)
            finished.add(items[c['sentido']])
        await remaining('fallido', 'mis_comprobantes_no_consultada',
                        'No se consultó: la identidad del contribuyente quedó en duda en la consulta anterior.')
        lineas = [f'{state.nombre} · Mis Comprobantes {nombre} · {state.mc_desde} a {state.mc_hasta}', *(linea(c) for c in consultas)]
        if sin_excel:
            lineas.append(sin_excel)
        if not envios:
            lineas.append('No se descargó ningún archivo.')
        if no_enviados:
            lineas.append('No se pudo enviar por Telegram: ' + ', '.join(no_enviados) + '. Están en Documentos del contribuyente, en el panel.')
        if state.mc_sentido != 'emitidos':
            lineas += fuente_compras(libros)
        # El resumen va una sola vez: el mensaje de avance se cierra con una línea corta y el
        # detalle llega en un único mensaje, después de los archivos.
        await flow._edit_progress(state, f'{state.nombre} · Mis Comprobantes {nombre}: consulta terminada.')
        await flow._send(adapter, chat, '\n'.join(lineas), thread)
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        await asyncio.shield(remaining('cancelado', 'cancelado_por_usuario', 'Consulta cancelada por el usuario.'))
        raise
    except (RuntimeError, ValueError, TypeError, OSError, TelegramError) as exc:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        code = str(exc) if re.fullmatch('[A-Z0-9_]{1,100}', str(exc)) else 'MC_ERROR_TECNICO'
        en_curso = code in ('MC_CONSULTA_EN_CURSO', 'PORTAL_IVA_EXECUTION_ALREADY_RUNNING')
        detalle = ('Ya hay una consulta de Mis Comprobantes en curso para este contribuyente. No se inició otra en ARCA.' if en_curso
                   else f'No se pudo completar Mis Comprobantes. {mensaje} No se modificó ARCA.' if mensaje
                   else 'No se pudo completar Mis Comprobantes. No se modificó ARCA. '
                        'Revisá el resultado en Operaciones; el diagnóstico quedó preservado.')
        await remaining('rechazado' if en_curso else 'fallido', code.lower(), detalle)
        await flow._edit_progress(state, 'Mis Comprobantes: la consulta no se completó.')
        await flow._send(adapter, chat, detalle, thread)
    finally:
        if proc is not None and proc.returncode is None:
            await flow._terminate_process(proc)
        flow.processes.pop(key, None)
        if locked:
            flow.execution_locks.pop(lock, None); flow._release_execution_lock(lock)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
