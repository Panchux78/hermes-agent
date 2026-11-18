"""ARCA Padrón ACT/Dom: selectores comunes, cola fiscal y entrega desde Documentos."""
import asyncio
import logging
import time
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow,DeduccionesScope
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_scope import FiscalScope
from plugins.platforms.telegram.menu_buttons import menu_label

log=logging.getLogger(__name__)

def resumen_padron(row):
    head='Padrón ACT/Dom · '+row['nombre']+' · '+row['periodo']
    if row['estado']!='terminada':return head+'\n'+row['progreso']
    r=row.get('resultado') or {}
    return (head+f"\n{r.get('compras',0)} compras · {r.get('ventas',0)} ventas · {r.get('excepciones',0)} excepciones."
        '\nRevisá la hoja Excepciones. Es referencia del padrón, no clasificación fiscal ni jurisdicción definitiva de IIBB. También queda en Documentos y Operaciones.')

class PadronActDomFlow(ComarbDeduccionesFlow):
    callback_prefix='pa'
    title='Padrón ACT/Dom'
    organismo='ARCA'
    back_page='om:arca_consultar'
    rechazos={'sin_permiso':'Tu usuario no tiene permiso para consultar el padrón.',
              'sin_acceso':'No encontré un contribuyente que puedas consultar. Probá con nombre, CUIT o slug.'}

    # Ese selector compartido sólo busca cartera y permisos: no exige clave ni
    # inscripción de COMARB y nunca abre una sesión del organismo.
    def _scope(self):return DeduccionesScope(self._query,'ARCA')
    def cancel_pending(self,chat_id,thread_id,user_id):self.states.pop(self._key(chat_id,thread_id,user_id),None)

    async def callback(self,adapter,query,data,chat_id,thread_id,user_id):
        if not data.startswith('pa:'):return False
        if str(chat_id)!=str(user_id):await query.answer('Esta consulta se realiza por privado.');return True
        parts=data.split(':')
        if len(parts)==3 and parts[1]=='stop' and parts[2].isascii() and parts[2].isdigit():
            actor=FiscalScope._telegram_id(user_id)
            try:r=await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_telegram({actor},'cancelar',{int(parts[2])})::text;")
            except Exception:r=[]
            await query.answer('Cancelación pedida.' if r and r[0] else 'No se pudo confirmar. Revisá Operaciones.');return True
        if len(parts)<2 or parts[1] in ('lote','cartera','cc'):
            await query.answer('Elegí un contribuyente para esta consulta.');return True
        return await super().callback(adapter,query,data,chat_id,thread_id,user_id)

    async def _submit_period(self,adapter,msg,state,key,selection):
        actor=FiscalScope._telegram_id(state.user_id);cid=int(state.contributor['id']);period=selection.visible[0]
        try:rows=await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_telegram({actor},'pedir',NULL,{cid},'{period}')::text;")
        except Exception:rows=[]
        if len(rows)!=1 or not isinstance(rows[0],dict):
            await msg.reply_text('No se pudo confirmar el pedido. Revisá Padrón ACT/Dom u Operaciones antes de repetirlo.');return True
        self.states.pop(key,None);jid=int(rows[0]['id_consulta'])
        head=self.title+' · '+state.contributor['nombre']+' · '+period
        message=await msg.reply_text(head+'\nVerificando Libros IVA conservados…',reply_markup=self._cancel(jid))
        self.watchers[jid]=asyncio.create_task(self._watch_padron(message,actor,jid,head))
        return True

    @staticmethod
    def _cancel(jid):return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'pa:stop:{jid}')]])

    async def _watch_padron(self,message,actor,jid,head):
        last='';deadline=time.monotonic()+1800
        try:
            while time.monotonic()<deadline:
                rows=await asyncio.to_thread(self._query,f"SELECT console.fn_padron_act_dom_telegram({actor},'estado',{jid})::text;")
                if len(rows)!=1 or not isinstance(rows[0],dict):return
                r=rows[0];terminal=r['estado'] in ('terminada','error','cancelada');text=head+'\n'+r['progreso']
                if text!=last:
                    await message.edit_text(text,reply_markup=None if terminal else self._cancel(jid));last=text
                if terminal:return
                await asyncio.sleep(3)
        except Exception as exc:log.warning('padron progreso no disponible consulta=%s clase=%s',jid,type(exc).__name__)
        finally:self.watchers.pop(jid,None)

    async def poll(self,adapter):
        rows=await asyncio.to_thread(self._query,'SELECT row_to_json(x)::text FROM console.fn_padron_act_dom_entregas() x;')
        for pending in rows:
            actor=int(pending['telegram_id']);jid=int(pending['id_consulta'])
            row=await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_telegram({actor},'reclamar',{jid})::text;")
            if len(row)!=1 or not isinstance(row[0],dict):continue
            row=row[0];message_id=None
            try:
                if row['estado']=='terminada':
                    a=row.get('archivo')
                    if not a:raise ValueError('archivo_no_disponible')
                    path=archivo_guardado(None,self.clients_root,a['ruta'],a['sha256'].strip())
                    # Reautorizar después de leer el archivo y antes de enviar.
                    fresh=await asyncio.to_thread(self._query,f"SELECT console.fn_padron_act_dom_telegram({actor},'estado',{jid})::text;")
                    if not fresh or not fresh[0] or fresh[0].get('archivo')!=a:raise ValueError('autorizacion_cambio')
                    delivery=await adapter.send_document(chat_id=str(actor),file_path=str(path),file_name=a['nombre'],caption=resumen_padron(row))
                    if not delivery.success:raise RuntimeError('telegram_no_confirmo')
                    message_id=getattr(delivery,'message_id',None)
                else:
                    sent=await adapter._bot.send_message(chat_id=actor,text=resumen_padron(row));message_id=sent.message_id
                value=str(int(message_id)) if message_id is not None else 'NULL'
                await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_entrega({actor},{jid},'enviada',{value})::text;")
            except Exception as exc:
                log.warning('padron entrega falló consulta=%s clase=%s',jid,type(exc).__name__)
                await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_entrega({actor},{jid},'error')::text;")
                await adapter._bot.send_message(chat_id=actor,text='Padrón ACT/Dom terminó, pero no pude confirmar el envío. Descargá el Excel desde Documentos en el panel.')

    async def run(self,adapter):
        while True:
            try:await self.poll(adapter)
            except Exception as exc:log.warning('padron entregador no disponible clase=%s',type(exc).__name__)
            await asyncio.sleep(3)
