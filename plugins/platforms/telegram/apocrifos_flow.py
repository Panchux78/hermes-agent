"""Proveedores apócrifos: mismo historial, worker y documento que el panel."""
import asyncio
import logging
import re

from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_scope import FiscalScope

log = logging.getLogger(__name__)
_ENTRY = re.compile(r'update:[0-9]{1,19}/mensaje:[1-9][0-9]{0,18}')
_MONTH = re.compile(r'(0[1-9]|1[0-2])/20[0-9]{2}')


def resultado(row):
    head = f"Proveedores apócrifos · {row['nombre']} · {row['periodo']}"
    if row['estado'] == 'no_verificada':
        return head + '\nNo se pudo verificar. ' + (row.get('motivo') or '') + '\nRevisá Historial en ARCA › Proveedores apócrifos.'
    text = (f"{head}\n{row['filas']} comprobantes · {row['proveedores']} proveedores · "
            f"{row['coincidencias']} proveedores en la nómina de ARCA.")
    if row['estado'] == 'parcial':
        text += f"\nRevisión parcial: {row['sin_cuit']} comprobantes sin CUIT verificable."
    return text + '\nEl mismo Excel queda en Documentos y Operaciones.'


class ApocrifosFlow(ComarbDeduccionesFlow):
    callback_prefix = 'apo'
    title = 'Proveedores apócrifos'
    organismo = 'ARCA'
    back_page = 'om:arca_consultar'
    rechazos = {'sin_permiso': 'Tu usuario no tiene permiso para ejecutar revisiones.',
                'sin_acceso': 'No encontré un contribuyente al que tengas acceso. Probá con nombre, CUIT o slug.'}

    def cancel_pending(self, chat_id, thread_id, user_id):
        self.states.pop(self._key(chat_id, thread_id, user_id), None)

    async def callback(self, adapter, query, data, chat_id, thread_id, user_id):
        if not data.startswith('apo:'):
            return False
        if str(chat_id) != str(user_id):
            await query.answer('Pedí la revisión en el chat privado con ContaBot.')
            return True
        action = data.split(':', 2)[1]
        if action not in {'uno', 'cancel', 'select', 'period'}:
            await query.answer('Volvé a abrir Proveedores apócrifos desde el menú de ARCA.')
            return True
        return await super().callback(adapter, query, data, chat_id, thread_id, user_id)

    async def text(self, adapter, msg, *, update_id=None):
        state = self.states.get(self._key(msg.chat_id, getattr(msg, 'message_thread_id', None), str(msg.from_user.id)))
        if state is not None and state.stage == 'periodo' and getattr(state, 'period_candidate', None) is None:
            state.apoc_entrada = f'update:{update_id}/mensaje:{getattr(msg, "message_id", None)}'
        return await super().text(adapter, msg)

    async def _submit_period(self, adapter, msg, state, key, selection):
        if self.states.get(key) is not state:
            return True
        actor = FiscalScope._telegram_id(state.user_id)
        cid = int(state.contributor['id'])
        period = selection.visible[0]
        entrada = getattr(state, 'apoc_entrada', '')
        if not _MONTH.fullmatch(period) or not _ENTRY.fullmatch(entrada):
            await msg.reply_text('No pude validar el pedido. Escribí el mes como MM/AAAA en este chat.')
            return True
        self.states.pop(key, None)
        try:
            rows = await asyncio.to_thread(self._write, 'SELECT console.fn_apoc_telegram_accion('
                f"{actor},'pedir',NULL,{cid},'{period}','{entrada}')::text;")
        except Exception:
            rows = []
        row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else {}
        if row.get('error') == 'en_curso':
            await msg.reply_text('Ese mes ya tiene una revisión en curso. Mirá ARCA › Proveedores apócrifos › Historial.', reply_markup=self._back())
        elif row.get('error') == 'periodo_futuro':
            state.period_candidate = None
            self.states[key] = state
            await msg.reply_text('Elegí el mes actual o uno anterior.')
        elif not row.get('id_revision'):
            await msg.reply_text('No pude confirmar el pedido. Mirá ARCA › Proveedores apócrifos › Historial antes de repetirlo.', reply_markup=self._back())
        else:
            await msg.reply_text(f"{self.title} · {state.contributor['nombre']} · {period}\n"
                'Revisión pedida. Te aviso por este chat cuando termine.', reply_markup=self._back())
        return True

    async def poll(self, adapter):
        rows = await asyncio.to_thread(self._query, 'SELECT row_to_json(x)::text FROM console.fn_apoc_telegram_entregas() x;')
        for pending in rows:
            actor, jid = int(pending['telegram_id']), int(pending['id_revision'])
            def accion(action, message='NULL'):
                return f"SELECT console.fn_apoc_telegram_accion({actor},'{action}',{jid},NULL,NULL,NULL,{message})::text;"
            results = await asyncio.to_thread(self._write, accion('reclamar'))
            if len(results) != 1 or not isinstance(results[0], dict):
                continue
            row = results[0]
            signed = f'SELECT console.fn_apoc_telegram_estado({actor},{jid})::text;'
            try:
                if row['estado'] == 'no_verificada':
                    if (await asyncio.to_thread(self._query, signed)) != [row]:
                        continue
                    sent = await adapter._bot.send_message(chat_id=actor, text=resultado(row))
                    message = int(sent.message_id)
                else:
                    a = row['archivo']
                    path = await asyncio.to_thread(archivo_guardado, None, self.clients_root, a['ruta'], a['sha256'].strip())
                    if (await asyncio.to_thread(self._query, signed)) != [row]:
                        continue
                    sent = await adapter.send_document(chat_id=str(actor), file_path=str(path), file_name=a['nombre'], caption=resultado(row))
                    if not sent.success or sent.delivered_filename != a['nombre']:
                        raise RuntimeError('telegram_no_confirmo')
                    message = int(sent.message_id)
                if message <= 0 or await asyncio.to_thread(self._write, accion('enviada', message)) != [True]:
                    raise RuntimeError('acuse_no_registrado')
            except Exception as exc:
                log.warning('apocrifos: entrega no confirmada revision=%s clase=%s', jid, type(exc).__name__)
                await asyncio.to_thread(self._write, accion('error'))
                if (await asyncio.to_thread(self._query, signed)) == [row]:
                    await adapter._bot.send_message(chat_id=actor, text='No pude confirmar la entrega. Mirá ARCA › Proveedores apócrifos › Historial o descargá el Excel desde Documentos.')

    async def run(self, adapter):
        while True:
            try:
                await self.poll(adapter)
            except Exception as exc:
                log.warning('apocrifos: entrega no disponible clase=%s', type(exc).__name__)
            await asyncio.sleep(5)
