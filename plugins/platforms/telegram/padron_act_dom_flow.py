"""ARCA Padrón ACT/Dom: selectores comunes, cola fiscal y entrega desde Documentos."""
import asyncio
import json
import logging
import time
import uuid
from urllib.parse import quote
from urllib.request import ProxyHandler,Request,build_opener
from urllib.error import HTTPError
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow,DeduccionesScope
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_scope import FiscalScope
from plugins.platforms.telegram.menu_buttons import menu_label

log=logging.getLogger(__name__)

def resumen_padron(row):
    r=row.get('resultado') or {}
    # #192 (22/11/2026): el Excel es el papel de trabajo de IVA e IIBB; el aviso cuenta sólo lo de IVA.
    papel=r.get('papel')=='Borrador IVA e IIBB'
    head=('Borrador IVA e IIBB' if papel else 'Padrón ACT/Dom')+' · '+row['periodo']+' · '+row['nombre']
    if row['estado']!='terminada':return head+'\n'+row['progreso']
    if papel:
        n,e=r.get('compras_clasificadas',0),r.get('excepciones_iva',0)
        texto=f"\n{n} {'compra clasificada' if n==1 else 'compras clasificadas'} y {e} {'excepción' if e==1 else 'excepciones'} para IVA."
        if r.get('faltantes'):texto+='\nFalta: '+'; '.join(r['faltantes'][:4])+'. Queda marcado FALTA en el papel.'
        return (head+texto+'\nRevisalo y corregí en las columnas amarillas. También queda en Documentos y Operaciones.'
                '\nPara devolverlo corregido, respondé a este mensaje adjuntando el Excel.')
    return (head+f"\n{r.get('compras',0)} compras · {r.get('ventas',0)} ventas · {r.get('excepciones',0)} excepciones."
        '\nRevisá la hoja Excepciones. También queda en Documentos y Operaciones.')

MAX_EXCEL=20*1024*1024


def resumen_devolucion(r):
    """Texto de la carga del Excel corregido (#192): lo que se tomó o qué celdas corregir."""
    if r.get('estado')=='aceptada':
        cuenta={'excluidas':0,'correcciones':0,'agregadas':0,'modificadas':0}
        datos=registrados=0
        for hoja,v in (r.get('resumen') or {}).items():
            if hoja=='BASES LIQ':datos+=v.get('datos',0)
            elif hoja=='AJUSTES':registrados+=v.get('registrados',0)
            else:
                for k in cuenta:cuenta[k]+=v.get(k,0)
        partes=[f'{n} {k}' for k,n in cuenta.items() if n]+([f'{datos} datos externos'] if datos else [])+([f'{registrados} ajustes globales'] if registrados else [])
        return ('Borrador IVA e IIBB · Excel corregido recibido\nSe tomaron tus cambios: '+(', '.join(partes) or 'sin cambios')
                +'.\nArmando el Borrador nuevo con ellos; te llega por acá.')
    errores=r.get('errores') or []
    lineas=[f"• {e.get('hoja') or 'Archivo'}"+(f" · fila {e['fila']}" if e.get('fila') else '')+(f" · {e['columna']}" if e.get('columna') else '')+f": {e.get('error')}"
            for e in errores[:15]]
    return ('Borrador IVA e IIBB · No se tomó ningún cambio.\nCorregí estas celdas y volvé a mandarlo respondiendo al mensaje del Borrador:\n'
            +'\n'.join(lineas)+(f'\nY {len(errores)-15} más.' if len(errores)>15 else ''))


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

    console_api_url='http://127.0.0.1:8000'

    def _enviar_devolucion(self,ticket,nombre,data):
        from urllib.parse import urlparse
        if urlparse(self.console_api_url).hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('devolucion_console_api_url_debe_ser_loopback')
        request=Request(self.console_api_url+'/api/internal/papel/devolucion',data=data,method='POST',
            headers={'X-Papel-Ticket':str(uuid.UUID(ticket)),'X-Archivo-Nombre':quote(nombre),'Content-Type':'application/octet-stream'})
        try:
            with build_opener(ProxyHandler({})).open(request,timeout=120) as response:
                return json.loads(response.read(2_000_000))
        except HTTPError as exc:
            try:detail=json.loads(exc.read(4096)).get('detail')
            except Exception:detail=None
            return {'estado':'error','detalle':detail if isinstance(detail,str) else None,'status':exc.code}

    async def document(self,adapter,msg):
        """#192: respuesta al mensaje del Borrador con el Excel corregido. Cualquier otro documento sigue de largo."""
        reply=getattr(msg,'reply_to_message',None);user=getattr(msg,'from_user',None);doc=getattr(msg,'document',None)
        if reply is None or user is None or doc is None or str(msg.chat_id)!=str(user.id):return False
        actor=FiscalScope._telegram_id(user.id)
        try:rows=await asyncio.to_thread(self._write,f"SELECT console.fn_papel_devolucion_ticket({actor},{int(reply.message_id)})::text;")
        except Exception:return False
        if len(rows)!=1 or not isinstance(rows[0],dict):return False
        t=rows[0]
        if t.get('estado')=='sin_permiso':
            await msg.reply_text('Tu usuario no puede cargar correcciones del Borrador IVA e IIBB.');return True
        if t.get('estado')!='ok':
            await msg.reply_text('Ese mensaje no tiene un Borrador IVA e IIBB terminado para corregir.');return True
        if not (doc.file_name or '').casefold().endswith('.xlsx') or not 0<(doc.file_size or 0)<=MAX_EXCEL:
            await msg.reply_text('Adjuntá el Borrador corregido en Excel (.xlsx), de hasta 20 MB.');return True
        try:
            data=bytes(await (await doc.get_file()).download_as_bytearray(read_timeout=30))
            r=await asyncio.to_thread(self._enviar_devolucion,t['ticket'],doc.file_name,data)
        except Exception as exc:
            log.warning('devolucion telegram no completada clase=%s',type(exc).__name__)
            await msg.reply_text('No pude leer el Excel. No se tomó ningún cambio; volvé a intentarlo respondiendo al mensaje del Borrador.');return True
        if r.get('estado') in ('aceptada','rechazada'):
            await msg.reply_text(resumen_devolucion(r))
        else:
            await msg.reply_text((r.get('detalle') if r.get('status') in (409,413,422) and r.get('detalle') else
                                  'No pude cargar el Excel corregido. No se tomó ningún cambio.'))
        return True

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
