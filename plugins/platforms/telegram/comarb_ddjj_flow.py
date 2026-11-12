"""DDJJ SIFERE presentadas: selector común, períodos explícitos y cola fiscal existente.

Sin evento LLM ni credenciales propias. La base vuelve a autorizar al encolar,
al ejecutar y al entregar. No presenta ni modifica declaraciones.
"""
from __future__ import annotations
import asyncio
import json
import logging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from plugins.platforms.telegram.comarb_deducciones_flow import (
    ComarbDeduccionesFlow, State, _STATE_TTL_SECONDS,
)
from plugins.platforms.telegram.comarb_ddjj_periodos import HELP, parse_periodos
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_scope import ACCOUNT_NOT_LINKED_MESSAGE, AccountNotLinked, FiscalScope
from plugins.platforms.telegram.menu_buttons import menu_label

logger = logging.getLogger(__name__)
TITLE = '🧾 DDJJ presentadas · SIFERE'


class ComarbPresentadasFlow(ComarbDeduccionesFlow):
    callback_prefix = 'dj'
    title = TITLE
    organismo = 'COMARB'
    back_page = 'om:comarb_presentadas'

    def cancel_pending(self, chat_id, thread_id, user_id):
        self.states.pop(self._key(chat_id, thread_id, user_id), None)

    def _cancel_keyboard(self, state):
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),
            callback_data=f'dj:cancel:{state.nonce}')]])

    async def _ask_period(self, adapter, chat_id, state, item):
        state.contributor = item
        state.stage = 'periodo'
        state.offer = None
        instruction = ('Ingresá un período MM/AAAA, por ejemplo 09/2026.'
                       if state.modo == 'periodo' else HELP)
        workbook = ('Un único Excel; sólo hojas de período.' if state.modo == 'periodo'
                    else 'Un único Excel con Resumen y hojas de período.')
        await adapter._bot.send_message(chat_id=chat_id,
            text=f"{item['nombre']}.\n{instruction}\n{workbook}",
            reply_markup=self._cancel_keyboard(state))

    async def callback(self, adapter, query, data, chat_id, thread_id, user_id):
        if not data.startswith('dj:'):
            return False
        if str(chat_id) != str(user_id):
            await query.answer('Esta consulta se realiza por privado.')
            return True
        parts = data.split(':')
        key = self._key(chat_id, thread_id, user_id)
        if len(parts) == 2 and parts[1] in ('periodo','lote'):
            self.states.pop(key, None)
            try:
                await asyncio.to_thread(self._scope().require_actor, user_id)
                await asyncio.to_thread(self._scope().require_permission, user_id, 'operaciones.ejecutar')
                await asyncio.to_thread(self._scope().require_permission, user_id, 'contribuyentes.editar')
            except AccountNotLinked:
                await query.answer('Cuenta no vinculada')
                await adapter._bot.send_message(chat_id=chat_id, text=ACCOUNT_NOT_LINKED_MESSAGE)
                return True
            except PermissionError:
                await query.answer('Sin permiso')
                return True
            except Exception:
                await query.answer('No pude verificar tu acceso. No se inició la consulta.')
                return True
            state = State(user_id, secrets.token_hex(4), 'buscar', parts[1])
            self.states[key] = state
            await query.answer()
            await adapter._bot.send_message(chat_id=chat_id,
                text=TITLE+'\nEscribí nombre, CUIT o slug del contribuyente.',
                reply_markup=self._cancel_keyboard(state))
            return True
        state = self.states.get(key)
        if state and time.monotonic() - state.created_at > _STATE_TTL_SECONDS:
            self.states.pop(key, None)
            state = None
        if len(parts) == 3 and parts[1] == 'cancel':
            if state is None or parts[2] != state.nonce:
                await query.answer('Ese pedido ya no está vigente.')
                return True
            self.states.pop(key, None)
            await query.answer('Cancelado')
            await adapter._bot.send_message(chat_id=chat_id, text='Cancelado. No se inició la consulta.', reply_markup=self._back())
            return True
        if len(parts) == 4 and parts[1] == 'select':
            if state is None or state.stage != 'buscar' or parts[2] != state.nonce:
                await query.answer('Esa selección ya no está vigente.')
                return True
            return await super().callback(adapter, query, data, chat_id, thread_id, user_id)
        if len(parts) == 3 and parts[1] == 'stop' and parts[2].isascii() and parts[2].isdigit():
            actor = FiscalScope._telegram_id(user_id)
            try:
                rows = await asyncio.to_thread(self._write,
                    f'SELECT console.fn_comarb_ddjj_cancelar_telegram({actor},{int(parts[2])})::text;')
            except Exception:
                rows = []
            await query.answer('Cancelación pedida' if rows == [True] else 'No pude confirmar la cancelación; revisá el estado del pedido.')
            return True
        await query.answer('Esa opción ya no está vigente. Abrí DDJJ presentadas.')
        return True

    async def text(self, adapter, msg):
        user_id = str(getattr(msg.from_user, 'id', ''))
        key = self._key(msg.chat_id, getattr(msg, 'message_thread_id', None), user_id)
        state = self.states.get(key)
        if state is None:
            return False
        if str(msg.chat_id) != user_id:
            return False
        if time.monotonic() - state.created_at > _STATE_TTL_SECONDS:
            self.states.pop(key, None)
            await msg.reply_text('El pedido venció: volvé a DDJJ presentadas. No se inició la consulta.')
            return True
        if state.stage == 'buscar':
            return await super().text(adapter, msg)
        try:
            periods = parse_periodos(msg.text, state.modo)
        except ValueError as exc:
            await msg.reply_text(str(exc))
            return True
        self.states.pop(key, None)  # single shot: nunca reencolar por un fallo de transporte
        actor = FiscalScope._telegram_id(user_id)
        payload = json.dumps(periods, separators=(',',':'))  # sólo meses ya validados
        try:
            rows = await asyncio.to_thread(self._write,
                f"SELECT console.fn_comarb_ddjj_pedir_telegram({actor},{int(state.contributor['id'])},'{payload}'::jsonb)::text;")
            answer = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
        except Exception:
            answer = None
        if answer is None:
            await msg.reply_text('No pude confirmar el registro del pedido. Revisá su estado antes de repetirlo.', reply_markup=self._back())
            return True
        if answer.get('estado') != 'encolada':
            messages = {**self.rechazos,
                'clave_sin_verificar':'No hay un acceso ARCA verificado, propio o de un representante autorizado. No se inició el ingreso.',
                'periodo_invalido':'Hay períodos inválidos o no cerrados; no se inició la consulta.'}
            await msg.reply_text(messages.get(answer.get('estado'), 'No se pudo iniciar la consulta. No se ingresó a SIFERE.'), reply_markup=self._back())
            return True
        jid = int(answer['id_lectura'])
        head = TITLE+' · '+state.contributor['nombre']
        summary = '\nIncluye la hoja Resumen con los meses del lote.' if len(periods) >= 2 else ''
        status = await msg.reply_text(head+'\nPeríodos: '+', '.join(periods)+
            '\nEn espera de la consulta. Recibirás un único Excel con las hojas MM-AAAA.'+summary,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'dj:stop:{jid}')]]))
        self.watchers[jid] = asyncio.create_task(self._watch(status, actor, jid, head, len(periods)))
        return True

    async def _watch(self, message, actor, jid, head, count):
        last = None
        end = time.monotonic() + 1200 * count + 60
        try:
            while time.monotonic() < end:
                await asyncio.sleep(3)
                try:
                    rows = await asyncio.to_thread(self._query,
                        f'SELECT console.fn_comarb_ddjj_estado_telegram({actor},{jid})::text;')
                except Exception:
                    continue  # sólo consulta local de estado, nunca repetición fiscal
                if len(rows) != 1 or not isinstance(rows[0], dict):
                    return
                state = rows[0]
                terminal = state['estado'] in ('terminada','error','cancelada')
                text = head+'\n'+(('Consulta finalizada: '+state['estado']+'. Se verificará la entrega.')
                    if terminal else (state.get('actual') or {}).get('progreso','En espera de SIFERE.'))
                if text != last:
                    await message.edit_text(text, reply_markup=None if terminal else message.reply_markup)
                    last = text
                if terminal:
                    return
        finally:
            self.watchers.pop(jid, None)

    async def close(self):
        self.states.clear()
        tasks = list(self.watchers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.watchers.clear()


def resumen_presentadas(row):
    if row['estado'] != 'terminada':
        return TITLE+'\n'+('Cancelada.' if row['estado'] == 'cancelada' else 'La consulta se interrumpió.')+' '+(row.get('motivo') or '')+' No se entregó un lote completo.'
    result = row.get('resultado') or {}
    periods = row.get('periodos') or []
    monthly = result.get('mensuales') or []
    if ([x.get('periodo') for x in monthly] != periods or result.get('periodos_solicitados') != periods
            or not periods or any(x.get('estado') not in ('presentada','sin_ddjj_verificada') for x in monthly)):
        raise ValueError('resultado_presentadas_incompleto')
    lines = [TITLE+' · '+row['nombre_legal'], '']
    for item in monthly:
        lines.append('• '+item['periodo']+(': sin DDJJ presentada, verificado en SIFERE.'
            if item['estado'] == 'sin_ddjj_verificada' else ': F'+str(item['formulario'])+' · secuencia '+str(item['secuencia'])))
    lines.append(f"Un único Excel, {len(periods)} hoja(s) de período; también en Documentos.")
    if len(periods) >= 2:
        lines.append('Incluye la hoja Resumen con los meses del lote.')
    return '\n'.join(lines)


class ComarbPresentadasEntregas:
    def __init__(self, flow):
        self.flow = flow

    async def poll(self, bot):
        rows = await asyncio.to_thread(self.flow._write,
            'SELECT row_to_json(e)::text FROM console.fn_comarb_ddjj_entregas_telegram() e;')
        for row in rows:
            if not isinstance(row,dict):
                continue
            jid, cid, chat = int(row['id_lectura']), int(row['id_contribuyente']), int(row['telegram_id'])
            token = secrets.token_hex(16)
            claimed = await asyncio.to_thread(self.flow._write,
                f"SELECT console.fn_comarb_ddjj_reclamar_telegram({chat},{jid},{cid},'{token}')::text;")
            if len(claimed) != 1 or not isinstance(claimed[0], dict):
                continue
            phase = claimed[0]['fase']

            async def transition(new, message=None, reason=None):
                nonlocal phase
                mid = 'NULL' if message is None else str(int(message.message_id))
                why = 'NULL' if reason is None else "'" + reason + "'"
                changed = await asyncio.to_thread(self.flow._write,
                    f"SELECT console.fn_comarb_ddjj_fase_telegram({chat},{jid},{cid},'{token}',"
                    f"'{phase}','{new}',{mid},{why})::text;")
                if changed != [True]:
                    raise PermissionError('entrega_sin_reclamo_vigente')
                phase = new

            async def current():
                value = await asyncio.to_thread(self.flow._query,
                    f'SELECT console.fn_comarb_ddjj_estado_telegram({chat},{jid})::text;')
                return len(value) == 1 and isinstance(value[0],dict)
            if not row.get('autorizado') or not await current():
                await transition('sin_permiso', reason='autorizacion_revocada')
                continue
            try:
                if phase == 'notificando_error':
                    notice = await bot.send_message(chat_id=chat, text=
                        'No pude confirmar el envío de DDJJ presentadas. Revisá Documentos; '
                        'el Excel no se reenvía automáticamente y no se repitió la consulta fiscal.')
                    await transition('error_notificado', notice)
                    continue
                text = resumen_presentadas(row)
                path = None
                if row['estado'] == 'terminada':
                    path = archivo_guardado(None,self.flow.clients_root,row.get('ruta'),row.get('sha256'))
                    if path is None or path.suffix.lower() != '.xlsx' or path.stat().st_size > 45_000_000:
                        raise ValueError('excel_no_verificado')
                if phase == 'reclamada':
                    if not await current():
                        await transition('sin_permiso', reason='autorizacion_revocada')
                        continue
                    await transition('enviando_aviso')
                    notice = await bot.send_message(chat_id=chat,text=text)
                    await transition('aviso_enviado', notice)
                if path is not None:
                    if not await current():
                        await transition('sin_permiso', reason='autorizacion_revocada')
                        continue
                    with path.open('rb') as stream:
                        await transition('enviando_archivo')
                        sheets = 'Resumen y hojas de período' if len(row['periodos']) >= 2 else 'sólo hojas de período'
                        sent = await bot.send_document(chat_id=chat,document=stream,filename=path.name,
                            caption='DDJJ presentadas · '+sheets+' · también en Documentos')
                    await transition('entregada', sent)
                else:
                    await transition('entregada')
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[COMARB-PRESENTADAS] entrega no completada; sin reenvío automático')
                # Si se perdió también la confirmación de DB, el lease conserva la fase
                # ambigua. Otro poll notifica el fallo, pero no reenvía el documento.
                try:
                    await transition('error', reason='envio_no_confirmado')
                except Exception:
                    logger.warning('[COMARB-PRESENTADAS] fase pendiente de conciliación local')

    async def run(self, adapter):
        while True:
            try:
                if adapter._bot is not None:
                    await self.poll(adapter._bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[COMARB-PRESENTADAS] entrega pendiente de verificación técnica')
            await asyncio.sleep(5)
