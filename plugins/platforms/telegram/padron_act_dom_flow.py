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
from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow,DeduccionesScope,State
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_scope import FiscalScope
from plugins.platforms.telegram.menu_buttons import menu_label

log=logging.getLogger(__name__)

XLS_VIEJO='Ese archivo es .xls. Guardalo como .xlsx y volvé a mandarlo respondiendo al mensaje del Borrador.'


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
        cola=('\nRevisalo y corregí en las columnas amarillas. También queda en Documentos y Operaciones.'
              '\nPara devolverlo corregido, respondé a este mensaje adjuntando el Excel.'
              +('\nPara cargar el detalle de ventas del cliente, respondé a este mensaje adjuntando su Excel (.xlsx).' if r.get('reparto') else ''))
        # El pie de un documento de Telegram admite 1.024 caracteres: primero se acorta el reparto y después lo que falta.
        rep=resumen_reparto(r)
        if len(head+texto+rep+cola)>1000:rep=rep.split('\n• ')[0].split('\nTodavía')[0]
        if len(head+texto+rep+cola)>1000:texto=texto[:max(0,1000-len(head+rep+cola)-1)]+'…'
        return head+texto+rep+cola
    return (head+f"\n{r.get('compras',0)} compras · {r.get('ventas',0)} ventas · {r.get('excepciones',0)} excepciones."
        '\nRevisá la hoja Excepciones. También queda en Documentos y Operaciones.')

def resumen_externo(row):
    r=row.get('resultado') or {}
    head='Padrón ACT/Dom · Lista externa · '+row['nombre']
    if row['estado']!='terminada':return head+'\n'+row['progreso']
    return (head+f"\n{r.get('filas',0)} filas · {r.get('verificados',0)} con respuesta verificada · "
        f"{r.get('para_revisar',r.get('invalidos',0)+r.get('sin_respuesta',0))} para revisar."
        '\nEl motivo de cada revisión está en Observaciones. También queda en Documentos y Operaciones.')


MAX_EXCEL=20*1024*1024


def _pesos(v):
    try:n=float(v)
    except (TypeError,ValueError):return str(v)
    return f'{n:,.2f}'.replace(',','X').replace('.',',').replace('X','.')


def resumen_reparto(r):
    """#240: reparto de ventas por actividad y Excel de importación de SIFERE del Borrador."""
    rep=r.get('reparto')
    if not rep:return ''
    if not rep.get('detalle'):
        return '\nReparto por actividad: falta el detalle de ventas del mes; todas las ventas quedaron en la actividad principal.'
    n=rep.get('con_reparto',0)
    texto=f"\nReparto por actividad: {n} {'venta repartida' if n==1 else 'ventas repartidas'} con el detalle del cliente."
    for a,v in (rep.get('totales') or {}).items():texto+=f'\n• {a}: $ {_pesos(v)}'
    s=rep.get('sin_reparto',0)
    if s:texto+=f"\n{s} {'venta sin reparto' if s==1 else 'ventas sin reparto'}: están en la hoja EXCEPCIONES."
    sif=r.get('sifere') or {}
    if sif.get('listo'):texto+='\nTe mando también el Excel para importar en SIFERE. No se importó ni se presentó nada.'
    elif sif.get('pendientes'):texto+='\nTodavía no hay Excel para importar en SIFERE: '+'; '.join(str(p) for p in sif['pendientes'][:3])
    return texto


def resumen_devolucion(r):
    """Texto de la carga del Excel corregido (#192): lo que se tomó o qué celdas corregir."""
    if r.get('tipo')=='detalle_ventas':
        # #240: lo cargado es el detalle de ventas del cliente, leído con el reparto de su ficha.
        if r.get('estado')=='aceptada':
            d=r.get('detalle') or {};n=d.get('comprobantes',0)
            partes=[f'• {a}: $ {_pesos(v)}' for a,v in (d.get('actividades') or {}).items()]
            return (f"Borrador IVA e IIBB · Detalle de ventas recibido\n{n} {'comprobante' if n==1 else 'comprobantes'} repartidos por actividad:\n"
                    +'\n'.join(partes)+'\nArmando el Borrador nuevo con ese reparto; te llega por acá.')
        errores=r.get('errores') or []
        lineas=[f"• {e.get('hoja') or 'Archivo'}"+(f" · fila {e['fila']}" if e.get('fila') else '')+(f" · {e['columna']}" if e.get('columna') else '')+f": {e.get('error')}"
                for e in errores[:15]]
        return ('Borrador IVA e IIBB · No se tomó el detalle de ventas.\nNo coincide con el formato que quedó en la ficha del cliente:\n'
                +'\n'.join(lineas)+(f'\nY {len(errores)-15} más.' if len(errores)>15 else ''))
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
        parts=data.split(':');actor=FiscalScope._telegram_id(user_id);key=self._key(chat_id,thread_id,user_id)
        if data=='pa:externo':
            try:
                await asyncio.to_thread(self._scope().require_actor,user_id)
                await asyncio.to_thread(self._scope().require_permission,user_id,'operaciones.ejecutar')
                rows=await asyncio.to_thread(self._write,f'SELECT row_to_json(x)::text FROM console.fn_padron_externo_estudios_telegram({actor}) x;')
            except Exception:rows=[]
            if not rows:
                await query.answer('No disponible')
                await adapter._bot.send_message(chat_id=chat_id,text='No pude verificar un estudio habilitado para esta consulta.');return True
            import secrets
            nonce=secrets.token_hex(4);state=State(user_id,nonce,'excel_externo','externo');self.states[key]=state
            if len(rows)==1:state.estudio_externo=int(rows[0]['id_estudio'])
            await query.answer()
            if len(rows)>1:
                state.stage='elegir_estudio_externo'
                keyboard=[[InlineKeyboardButton(r['nombre'],callback_data=f"pa:estudio:{r['id_estudio']}:{nonce}")] for r in rows]
                await adapter._bot.send_message(chat_id=chat_id,text='Elegí el estudio para el padrón externo.',reply_markup=InlineKeyboardMarkup(keyboard));return True
            await self._pedir_excel(adapter,chat_id,state);return True
        if len(parts)==4 and parts[1]=='estudio' and parts[2].isdigit():
            state=self.states.get(key)
            if not state or state.stage!='elegir_estudio_externo' or parts[3]!=state.nonce:
                await query.answer('Esa selección ya no está vigente.');return True
            state.estudio_externo=int(parts[2]);state.stage='excel_externo'
            await query.answer();await self._pedir_excel(adapter,chat_id,state);return True
        if len(parts)==3 and parts[1]=='stopx' and parts[2].isascii() and parts[2].isdigit():
            try:r=await asyncio.to_thread(self._write,f"SELECT console.fn_padron_externo_telegram({actor},'cancelar',{int(parts[2])})::text;")
            except Exception:r=[]
            await query.answer('Cancelación pedida.' if r and r[0] else 'No se pudo confirmar. Revisá Operaciones.');return True
        if len(parts)==3 and parts[1]=='stop' and parts[2].isascii() and parts[2].isdigit():
            try:r=await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_telegram({actor},'cancelar',{int(parts[2])})::text;")
            except Exception:r=[]
            await query.answer('Cancelación pedida.' if r and r[0] else 'No se pudo confirmar. Revisá Operaciones.');return True
        if len(parts)<2 or parts[1] in ('lote','cartera','cc'):
            await query.answer('Elegí un contribuyente para esta consulta.');return True
        return await super().callback(adapter,query,data,chat_id,thread_id,user_id)

    async def _pedir_excel(self,adapter,chat_id,state):
        await adapter._bot.send_message(chat_id=chat_id,
            text='Adjuntá un Excel .xlsx con una columna llamada CUIT o CUIL, con o sin guiones. Hasta 20 MB.\nSe conserva el orden y recibís domicilio, actividades, condición frente al IVA y observaciones.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'pa:cancel:{state.nonce}')]]))

    def _enviar_externo(self,ticket,nombre,data):
        from urllib.parse import urlparse
        parsed=urlparse(self.console_api_url)
        if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','::1') or parsed.port is None
                or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError('padron_externo_console_api_url_debe_ser_loopback')
        request=Request(self.console_api_url+'/api/internal/padron-act-dom/externo',data=data,method='POST',
            headers={'X-Padron-Ticket':str(uuid.UUID(ticket)),'X-Archivo-Nombre':quote(nombre),'Content-Type':'application/octet-stream'})
        try:
            with build_opener(ProxyHandler({})).open(request,timeout=120) as response:return json.loads(response.read(2_000_000))
        except HTTPError as exc:
            try:detail=json.loads(exc.read(4096)).get('detail')
            except Exception:detail=None
            return {'estado':'error','detalle':detail if isinstance(detail,str) else None,'status':exc.code}

    async def _documento_externo(self,adapter,msg,state,key,actor):
        doc=msg.document
        if not (doc.file_name or '').casefold().endswith('.xlsx') or not 0<(doc.file_size or 0)<=MAX_EXCEL:
            await msg.reply_text('Adjuntá la lista en Excel .xlsx, de hasta 20 MB.');return True
        try:
            data=bytes(await (await doc.get_file()).download_as_bytearray(read_timeout=30))
            rows=await asyncio.to_thread(self._write,f"SELECT json_build_object('ticket',console.fn_padron_externo_ticket({actor},{state.estudio_externo},'telegram'))::text;")
            if len(rows)!=1 or not isinstance(rows[0],dict) or not rows[0].get('ticket'):raise ValueError('ticket_no_disponible')
            result=await asyncio.to_thread(self._enviar_externo,str(rows[0]['ticket']),doc.file_name,data)
            jid=int(result['id_consulta'])
        except Exception as exc:
            log.warning('padron externo telegram no completado clase=%s',type(exc).__name__)
            await msg.reply_text('No pude cargar el Excel. No se inició una consulta nueva; revisá el archivo y volvé a intentarlo.');return True
        self.states.pop(key,None);head='Padrón ACT/Dom · Lista externa'
        message=await msg.reply_text(head+'\nLeyendo el Excel externo…')
        self.watchers[jid]=asyncio.create_task(self._watch_padron(message,actor,jid,head,externo=True))
        return True

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

    @staticmethod
    def _cancel_externo(jid):return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'pa:stopx:{jid}')]])

    async def _watch_padron(self,message,actor,jid,head,externo=False):
        last='';deadline=time.monotonic()+1800
        try:
            while time.monotonic()<deadline:
                function='fn_padron_externo_telegram' if externo else 'fn_padron_act_dom_telegram'
                rows=await asyncio.to_thread(self._query,f"SELECT console.{function}({actor},'estado',{jid})::text;")
                if len(rows)!=1 or not isinstance(rows[0],dict):return
                r=rows[0];terminal=r['estado'] in ('terminada','error','cancelada');text=head+'\n'+r['progreso']
                if text!=last:
                    await message.edit_text(text,reply_markup=None if terminal else (self._cancel_externo(jid) if externo else self._cancel(jid)));last=text
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
        """Acepta el Excel externo o, respondiendo al Borrador, el Excel corregido."""
        reply=getattr(msg,'reply_to_message',None);user=getattr(msg,'from_user',None);doc=getattr(msg,'document',None)
        if user is None or doc is None or str(msg.chat_id)!=str(user.id):return False
        actor=FiscalScope._telegram_id(user.id)
        state=self.states.get(self._key(msg.chat_id,getattr(msg,'message_thread_id',None),user.id))
        if state and state.stage=='excel_externo' and getattr(state,'estudio_externo',None):
            return await self._documento_externo(adapter,msg,state,self._key(msg.chat_id,getattr(msg,'message_thread_id',None),user.id),actor)
        if reply is None:return False
        try:rows=await asyncio.to_thread(self._write,f"SELECT console.fn_papel_devolucion_ticket({actor},{int(reply.message_id)})::text;")
        except Exception:return False
        if len(rows)!=1 or not isinstance(rows[0],dict):return False
        t=rows[0]
        if t.get('estado')=='sin_permiso':
            await msg.reply_text('Tu usuario no puede cargar correcciones del Borrador IVA e IIBB.');return True
        if t.get('estado')!='ok':
            await msg.reply_text('Ese mensaje no tiene un Borrador IVA e IIBB terminado para corregir.');return True
        if (doc.file_name or '').casefold().endswith('.xls'):
            # #240 (contadora, 27/11/2026): un .xls no se lee ni se rechaza en silencio.
            await msg.reply_text(XLS_VIEJO);return True
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
                    # #240: el Excel de importación de actividades de SIFERE que salió de este Borrador.
                    sif=(row.get('resultado') or {}).get('sifere') or {}
                    if sif.get('listo') and sif.get('ruta') and sif.get('sha256'):
                        try:
                            extra=archivo_guardado(None,self.clients_root,sif['ruta'],str(sif['sha256']).strip())
                            sent=await adapter.send_document(chat_id=str(actor),file_path=str(extra),file_name=sif.get('nombre') or 'sifere-actividades.xlsx',
                                caption='Actividades para SIFERE · '+row['periodo']+' · '+row['nombre']+'\nExcel para importar en SIFERE Web. ContaBot no importó ni presentó nada.')
                            if not sent.success:raise RuntimeError('telegram_no_confirmo')
                        except Exception as exc:
                            log.warning('padron sifere no enviado consulta=%s clase=%s',jid,type(exc).__name__)
                            await adapter._bot.send_message(chat_id=actor,text='El Excel para importar en SIFERE quedó en Documentos del panel; no pude enviarlo por acá.')
                else:
                    sent=await adapter._bot.send_message(chat_id=actor,text=resumen_padron(row));message_id=sent.message_id
                value=str(int(message_id)) if message_id is not None else 'NULL'
                await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_entrega({actor},{jid},'enviada',{value})::text;")
            except Exception as exc:
                log.warning('padron entrega falló consulta=%s clase=%s',jid,type(exc).__name__)
                await asyncio.to_thread(self._write,f"SELECT console.fn_padron_act_dom_entrega({actor},{jid},'error')::text;")
                await adapter._bot.send_message(chat_id=actor,text='Padrón ACT/Dom terminó, pero no pude confirmar el envío. Descargá el Excel desde Documentos en el panel.')
        await self.poll_externos(adapter)

    async def poll_externos(self,adapter):
        rows=await asyncio.to_thread(self._query,'SELECT row_to_json(x)::text FROM console.fn_padron_externo_entregas() x;')
        for pending in rows:
            actor=int(pending['telegram_id']);jid=int(pending['id_consulta'])
            row=await asyncio.to_thread(self._write,f"SELECT console.fn_padron_externo_telegram({actor},'reclamar',{jid})::text;")
            if len(row)!=1 or not isinstance(row[0],dict):continue
            row=row[0];message_id=None
            try:
                if row['estado']=='terminada':
                    a=row.get('archivo')
                    if not a:raise ValueError('archivo_no_disponible')
                    path=archivo_guardado(None,self.clients_root,a['ruta'],a['sha256'].strip())
                    fresh=await asyncio.to_thread(self._query,f"SELECT console.fn_padron_externo_telegram({actor},'estado',{jid})::text;")
                    if not fresh or not fresh[0] or fresh[0].get('archivo')!=a:raise ValueError('autorizacion_cambio')
                    delivery=await adapter.send_document(chat_id=str(actor),file_path=str(path),file_name=a['nombre'],caption=resumen_externo(row))
                    if not delivery.success:raise RuntimeError('telegram_no_confirmo')
                    message_id=getattr(delivery,'message_id',None)
                else:
                    sent=await adapter._bot.send_message(chat_id=actor,text=resumen_externo(row));message_id=sent.message_id
                value=str(int(message_id)) if message_id is not None else 'NULL'
                await asyncio.to_thread(self._write,f"SELECT console.fn_padron_externo_entrega({actor},{jid},'enviada',{value})::text;")
            except Exception as exc:
                log.warning('padron externo entrega falló consulta=%s clase=%s',jid,type(exc).__name__)
                await asyncio.to_thread(self._write,f"SELECT console.fn_padron_externo_entrega({actor},{jid},'error')::text;")
                await adapter._bot.send_message(chat_id=actor,text='Padrón externo terminó, pero no pude confirmar el envío. Descargá el Excel desde Documentos en el panel.')

    async def run(self,adapter):
        while True:
            try:await self.poll(adapter)
            except Exception as exc:log.warning('padron entregador no disponible clase=%s',type(exc).__name__)
            await asyncio.sleep(3)
