"""Conciliación con el mayor: selector común, período común y motor único en la cola."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import subprocess
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import CONTRIBUTOR_PROMPT, ContributorOffer, MULTIPLE_CONTRIBUTORS_TEXT
from plugins.platforms.telegram.cruce_iva_flow import CruceIvaFlow
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.period_input import SPECS, period_callback, propose_period
from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow

log = logging.getLogger(__name__)
TITLE = 'Conciliación con el mayor'
FIELDS = {'fecha': 'Fecha', 'referencia': 'Comprobante', 'detalle': 'Detalle', 'debe': 'Debe',
          'haber': 'Haber', 'importe': 'Importe con signo', 'saldo': 'Saldo', 'identidad': 'CUIT o proveedor',
          'medio': 'Medio de pago', 'emision': 'Fecha de emisión', 'cuenta': 'Cuenta', 'moneda': 'Moneda', 'no_usar': 'No usar'}


@dataclass
class State:
    nonce: str
    user: str
    stage: str = 'search'
    updated: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.monotonic)
    candidate: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)
    datos: dict = field(default_factory=dict)
    id: int | None = None
    period_candidate: object | None = None
    columns: list = field(default_factory=list)
    mapping: dict = field(default_factory=dict)
    preview: dict = field(default_factory=dict)
    column_index: str | None = None
    task: asyncio.Task | None = None


class ConciliacionMayorFlow:
    TTL = 15*60
    POLL_SECONDS = 2

    def __init__(self):
        self.states = {}

    _key = staticmethod(CruceIvaFlow._key)
    _search = classmethod(lambda cls, uid, term: CruceIvaFlow._search(uid, term))

    @staticmethod
    def _request(uid, action, datos):
        # Reutiliza la configuración del transporte restringido. El archivo va por stdin,
        # no por -c/argv (un Excel excede ARG_MAX y no debe aparecer en ps).
        args, env = VencimientosFlow._connection_args()
        payload = base64.b64encode(json.dumps(datos, ensure_ascii=False).encode()).decode()
        sql = (f"SELECT console.fn_conciliacion_telegram({int(uid)},'{action}',"
               f"convert_from(decode('{payload}','base64'),'UTF8')::jsonb)::text;")
        if action not in {'crear', 'planillas', 'extracto', 'archivo', 'iniciar', 'columnas', 'cancelar', 'estado'}:
            raise ValueError('accion_invalida')
        r = subprocess.run(args, input=sql, env=env, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=60, check=False)
        if r.returncode:
            raise RuntimeError('conciliacion_no_disponible')
        return json.loads(r.stdout)

    @staticmethod
    def _keyboard(state, entries):
        rows = [[InlineKeyboardButton(menu_label(icon, label), callback_data=f'cm:{action}:{state.nonce}')]
                for icon, label, action in entries]
        rows.append([InlineKeyboardButton(menu_label('✕', 'Cancelar'), callback_data=f'cm:cancel:{state.nonce}')])
        return InlineKeyboardMarkup(rows)

    def cancel_pending(self, chat_id, thread_id, user_id):
        key = self._key(chat_id, thread_id, user_id)
        state = self.states.get(key)
        if state and state.stage != 'running':
            self.states.pop(key)
            return True
        return False

    async def _say(self, target, state, text, entries=()):
        await target.reply_text(f'{TITLE}\n{text}', reply_markup=self._keyboard(state, entries))

    async def _pick_source(self, target, state):
        rows = await asyncio.to_thread(self._request, state.user, 'planillas', {'id_contribuyente': state.candidate['id']})
        state.candidates = {int(r['id']): r for r in rows}
        state.stage = 'source'
        entries = [('📄', str(r['nombre'])[:65], f'bank:{r["id"]}') for r in rows[:20]]
        entries.append(('📤', 'Subir otro extracto XLSX', 'upload'))
        await self._say(target, state, f'{state.candidate["nombre"]}\nElegí el extracto de esta cuenta o subí otro. Si no figura entre los últimos 20, podés subirlo.', entries)

    async def callback(self, adapter, query, data, chat_id, thread_id, user_id):
        if not data.startswith('cm:'):
            return False
        key = self._key(chat_id, thread_id, user_id)
        state = self.states.get(key)
        if data == 'cm:start':
            if state and state.stage == 'running':
                await query.answer('Hay una conciliación en curso. Podés cancelarla o esperar el resultado.')
                return True
            state = State(uuid.uuid4().hex[:10], str(user_id))
            self.states[key] = state
            await query.answer()
            await self._say(query.message, state, CONTRIBUTOR_PROMPT)
            return True
        if data.startswith('cm:period:') and state:
            return await period_callback(state, query, data, prefix='cm', is_current=lambda: self.states.get(key) is state,
                                         discard=lambda: self.states.pop(key, None))
        parts = data.split(':')
        # La selección de contribuyente usa el contrato común cm:select:nonce:id.
        nonce = parts[2] if len(parts) >= 3 and parts[1] in ('select', 'cancel') else parts[-1]
        if state is None or state.nonce != nonce or time.monotonic()-state.updated > self.TTL:
            await query.answer('Este pedido venció. Volvé a Bancos › Conciliación con el mayor.')
            return True
        await query.answer()
        state.updated = time.monotonic()
        try:
            await self._action(adapter, query.message, state, key, parts, chat_id, thread_id)
        except Exception as exc:
            log.warning('conciliacion callback fallida tipo=%s', type(exc).__name__)
            await self._say(query.message, state, 'No pude completar ese paso. No se generó una conciliación; los archivos recibidos se conservan. Podés cancelarlo y volver a empezar.')
        return True

    async def _action(self, adapter, target, state, key, parts, chat_id, thread_id):
        action = parts[1]
        if action == 'cancel':
            if state.id:
                await asyncio.to_thread(self._request, state.user, 'cancelar', {'id': state.id})
            if state.task:
                state.task.cancel()
            self.states.pop(key, None)
            await target.reply_text('Conciliación cancelada. Los originales quedan conservados.')
        elif action == 'select' and state.stage == 'search':
            candidate = state.candidates.get(int(parts[3]))
            if candidate is None:
                raise ValueError('contribuyente_no_ofrecido')
            state.candidate, state.stage = candidate, 'period'
            state.created_at = time.monotonic()
            await self._say(target, state, f'{candidate["nombre"]}\n'+SPECS['conciliacion-mayor'].prompt)
        elif action == 'currency' and state.stage == 'currency' and parts[2] in ('ARS', 'USD'):
            state.datos['moneda'], state.stage = parts[2], 'origin'
            await self._say(target, state, '¿De qué sistema u origen viene el mayor? Uso ese nombre para recordar las columnas.')
        elif action == 'upload' and state.stage == 'source':
            state.stage = 'bank_upload'
            await self._say(target, state, 'Mandame el extracto XLSX de Resumen bancario → Excel como documento (hasta 20 MB).')
        elif action == 'bank' and state.stage == 'source':
            doc = int(parts[2])
            if doc not in state.candidates:
                raise ValueError('extracto_no_ofrecido')
            await asyncio.to_thread(self._request, state.user, 'extracto', {'id': state.id, 'banco_id': doc})
            state.stage = 'ledger_upload'
            await self._say(target, state, 'Mandame el mayor de esta cuenta Banco, XLSX o CSV, como documento (hasta 20 MB).')
        elif action == 'column' and state.stage == 'columns':
            if parts[2] not in state.mapping:
                raise ValueError('columna_no_ofrecida')
            state.column_index, state.stage = parts[2], 'field'
            await self._say(target, state, '¿Qué contiene esta columna?', [('📋', label, f'field:{name}') for name, label in FIELDS.items()])
        elif action == 'field' and state.stage == 'field' and parts[2] in FIELDS:
            state.mapping[state.column_index], state.stage = parts[2], 'columns'
            await self._show_columns(target, state)
        elif action == 'mapped' and state.stage == 'columns':
            await asyncio.to_thread(self._request, state.user, 'columnas', {'id': state.id, 'opciones':
                {'mapeo': state.mapping, 'hoja': state.preview['hoja'], 'formato': state.datos.get('formato','ar')}})
            self._start_poll(adapter, target, state, key, chat_id, thread_id)
        elif action == 'format' and state.stage == 'columns' and parts[2] in ('ar','us'):
            state.datos['formato'] = parts[2]
            await self._show_columns(target, state)
        elif action == 'sheet' and state.stage == 'columns':
            index = int(parts[2])
            if not 0 <= index < len(state.preview['hojas']):
                raise ValueError('hoja_no_ofrecida')
            await asyncio.to_thread(self._request,state.user,'columnas',{'id':state.id,'opciones':
                {'hoja':state.preview['hojas'][index]}})
            self._start_poll(adapter,target,state,key,chat_id,thread_id)

    async def text(self, adapter, message):
        uid = str(getattr(getattr(message, 'from_user', None), 'id', ''))
        thread = getattr(message, 'message_thread_id', None)
        key = self._key(message.chat_id, thread, uid)
        state = self.states.get(key)
        if state is None:
            return False
        if time.monotonic()-state.updated > self.TTL:
            self.states.pop(key, None)
            await message.reply_text('La conciliación venció. Volvé a iniciarla desde Bancos.')
            return True
        state.updated = time.monotonic()
        raw = (message.text or '').strip()
        try:
            await self._text(adapter, message, state, raw)
        except ValueError as exc:
            await self._say(message, state, str(exc))
        except Exception as exc:
            log.warning('conciliacion paso fallido tipo=%s', type(exc).__name__)
            await self._say(message, state, 'No pude completar ese paso. No se inició otra conciliación. Podés cancelar y volver a empezar.')
        return True

    async def _text(self, adapter, message, state, raw):
        if state.stage == 'search':
            rows = await asyncio.to_thread(self._search, int(state.user), raw)
            offer = ContributorOffer.from_rows(rows)
            if offer.status == 'empty':
                await self._say(message, state, 'No encontré un cliente autorizado con ese nombre, CUIT o alias.')
            elif offer.status == 'multiple':
                state.candidates = offer.candidates
                await message.reply_text(MULTIPLE_CONTRIBUTORS_TEXT, reply_markup=offer.keyboard(callback_prefix='cm', nonce=state.nonce, cancel_text=menu_label('✕','Cancelar')))
            else:
                state.candidate, state.stage = offer.single, 'period'
                state.created_at = time.monotonic()
                await self._say(message, state, f'{state.candidate["nombre"]}\n'+SPECS['conciliacion-mayor'].prompt)
        elif state.stage == 'period':
            async def submit(selection):
                start, end = selection.dates()
                state.datos.update(desde=datetime.strptime(start,'%d/%m/%Y').date().isoformat(), hasta=datetime.strptime(end,'%d/%m/%Y').date().isoformat())
                state.stage = 'account'
                await self._say(message, state, '¿Cuál es la cuenta Banco del mayor? Ingresá un alias recordable, sin CBU.')
            await propose_period(state, 'conciliacion-mayor', raw, prefix='cm', send=message.reply_text, submit=submit)
        elif state.stage == 'account':
            if not 1 <= len(raw) <= 100:
                raise ValueError('La cuenta debe tener entre 1 y 100 caracteres.')
            state.datos['cuenta'], state.stage = raw, 'currency'
            await self._say(message, state, '¿Qué moneda tienen las dos fuentes?', [('💵','Pesos','currency:ARS'),('💵','Dólares','currency:USD')])
        elif state.stage == 'origin':
            if not 1 <= len(raw) <= 100:
                raise ValueError('El origen debe tener entre 1 y 100 caracteres.')
            state.datos.update(origen=raw, id_contribuyente=state.candidate['id'])
            result = await asyncio.to_thread(self._request, state.user, 'crear', state.datos)
            state.id = int(result['id'])
            await self._pick_source(message, state)
        else:
            await self._say(message, state, 'Seguí el paso indicado: mandá el archivo como documento o tocá una de las opciones. Podés cancelar este pedido.')

    async def document(self, adapter, message):
        uid = str(getattr(getattr(message, 'from_user', None), 'id', ''))
        thread = getattr(message, 'message_thread_id', None)
        key = self._key(message.chat_id, thread, uid)
        state = self.states.get(key)
        if state is None:
            return False
        if state.stage not in ('bank_upload', 'ledger_upload'):
            await self._say(message, state, 'Todavía no espero un archivo. Completá primero el paso indicado.')
            return True
        doc = message.document
        bank = state.stage == 'bank_upload'
        name = str(doc.file_name or '')
        if (not name.lower().endswith(('.xlsx',) if bank else ('.xlsx','.csv')) or not doc.file_size
                or doc.file_size > 20*1024*1024):
            await self._say(message, state, 'El extracto debe ser XLSX y el mayor XLSX o CSV, de hasta 20 MB.')
            return True
        state.stage = 'receiving'
        try:
            file = await doc.get_file()
            content = bytes(await file.download_as_bytearray())
            if not 0 < len(content) <= 20*1024*1024:
                raise ValueError('archivo_tamano_invalido')
            await asyncio.to_thread(self._request, state.user, 'archivo', {'id': state.id, 'lado': 'banco' if bank else 'mayor',
                                                                         'nombre': name, 'contenido': base64.b64encode(content).decode()})
            if bank:
                state.stage = 'ledger_upload'
                await self._say(message, state, 'Extracto recibido. Ahora mandame el mayor de esta cuenta Banco, XLSX o CSV.')
            else:
                await asyncio.to_thread(self._request, state.user, 'iniciar', {'id': state.id})
                self._start_poll(adapter, message, state, key, message.chat_id, thread)
        except Exception as exc:
            state.stage = 'bank_upload' if bank else 'ledger_upload'
            log.warning('conciliacion archivo fallido tipo=%s', type(exc).__name__)
            await self._say(message, state, 'No pude recibir el archivo. El paso sigue abierto; podés mandarlo de nuevo o cancelar.')
        return True

    def _start_poll(self, adapter, target, state, key, chat_id, thread_id):
        state.stage = 'running'
        state.task = asyncio.create_task(self._poll(adapter, target, state, key, chat_id, thread_id))

    async def _show_columns(self, target, state):
        text = ['Indicá qué contiene cada columna. Las reconocidas ya están completas.']
        if state.preview.get('motivo'):
            text.append(state.preview['motivo'])
        entries = []
        for c in state.columns:
            label = FIELDS[state.mapping.get(c['indice'], 'no_usar')]
            text.append(f'{c["encabezado"]}: {label}. Muestras: '+ ' / '.join(c['muestras'])[:180])
            entries.append(('📋', c['encabezado'][:35]+' → '+label, 'column:'+c['indice']))
        entries.extend([('📑',s[:50],f'sheet:{i}') for i,s in enumerate(state.preview['hojas']) if s!=state.preview['hoja']])
        entries.extend([('🔢','Coma decimal (1.234,56)','format:ar'),('🔢','Punto decimal (1,234.56)','format:us')])
        entries.append(('✅','Guardar columnas y conciliar','mapped'))
        await self._say(target, state, '\n'.join(text)[:3500], entries)

    async def _poll(self, adapter, target, state, key, chat_id, thread_id):
        try:
            progress = await target.reply_text('Conservando originales y conciliando…', reply_markup=self._keyboard(state, []))
            deadline = time.monotonic()+600
            while time.monotonic() < deadline:
                status = await asyncio.to_thread(self._request, state.user, 'estado', {'id': state.id})
                if status['estado'] == 'columnas':
                    state.preview = status['vista_previa']
                    state.columns, state.mapping, state.stage = state.preview['columnas'], dict(state.preview['mapeo']), 'columns'
                    await self._show_columns(target, state)
                    return
                if status['estado'] in ('terminada','error','cancelada'):
                    await progress.edit_text(status.get('error') or (status.get('resultado') or {}).get('estado') or 'Conciliación cancelada.')
                    if status['estado'] == 'terminada':
                        # Mismo resolvedor seguro y entregador de Excel del cruce ya operativo.
                        output = CruceIvaFlow()._output_path(status['salida_relativa'])
                        result = status['resultado']
                        sent = await adapter.send_document(chat_id=str(chat_id), file_path=str(output), file_name=output.name,
                            caption=f'{TITLE} · {state.candidate["nombre"]}\n{result["estado"]}. {result["coincidencias"]} coincidencias; {len(result["pendientes"])} partidas pendientes.',
                            metadata={'thread_id': thread_id} if thread_id is not None else None)
                        if not getattr(sent, 'success', False):
                            await target.reply_text('El Excel quedó en Documentos, pero Telegram no confirmó la entrega. No hace falta volver a conciliar.')
                    self.states.pop(key, None)
                    return
                await asyncio.sleep(self.POLL_SECONDS)
            await target.reply_text('La conciliación sigue en proceso. Revisá su estado en Bancos › Conciliación con el mayor del panel; no se inició otra.')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('conciliacion seguimiento fallido tipo=%s', type(exc).__name__)
            await target.reply_text('No pude seguir el avance. El pedido quedó registrado en Operaciones y sus archivos en Documentos; no se inició otro.')
        finally:
            if state.stage == 'running':
                self.states.pop(key, None)
