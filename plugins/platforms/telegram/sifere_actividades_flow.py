"""Preparación art.2: selector y período comunes, misma cola y entregador COMARB."""
import asyncio
import json
import re
import time
from telegram import InlineKeyboardButton,InlineKeyboardMarkup
from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow,_STATE_TTL_SECONDS
from plugins.platforms.telegram.fiscal_scope import FiscalScope
from plugins.platforms.telegram.menu_buttons import menu_label

TITLE='📄 Actividades para SIFERE'

def resumen_actividades(row):
    r=row.get('resultado') or {}
    head=TITLE+' · '+row['nombre_legal']+' · '+r.get('periodo','')
    if row['estado']!='terminada':return head+'\n'+('Cancelada.' if row['estado']=='cancelada' else 'No se pudo preparar.')+' '+(row.get('motivo') or '')
    lines=[head,'',('Lista para importar.' if r.get('listo') else 'Requiere revisión. No hay archivo listo para importar.'),
      f"{r.get('ventas',0)} comprobantes de ventas · {r.get('jurisdicciones',0)} jurisdicciones."]
    for p in (r.get('pendientes') or [])[:4]:lines.append('• '+p['detalle'])
    if len(r.get('pendientes') or [])>4:lines.append('El control adjunto contiene todas las observaciones.')
    lines.append('No se importó ni presentó ninguna DDJJ. También está en Documentos.')
    return '\n'.join(lines)

class SifereActividadesFlow(ComarbDeduccionesFlow):
    callback_prefix='sa'
    title=TITLE
    back_page='om:comarb_consultar'

    def cancel_pending(self,chat_id,thread_id,user_id):self.states.pop(self._key(chat_id,thread_id,user_id),None)

    async def callback(self,adapter,query,data,chat_id,thread_id,user_id):
        if not data.startswith('sa:'):return False
        if str(chat_id)!=str(user_id):await query.answer('Esta preparación se realiza por privado.');return True
        key=self._key(chat_id,thread_id,user_id);state=self.states.get(key)
        if state and time.monotonic()-state.created_at>_STATE_TTL_SECONDS:self.states.pop(key,None);state=None
        parts=data.split(':')
        if len(parts)==3 and parts[1]=='stop' and parts[2].isascii() and parts[2].isdigit():
            actor=FiscalScope._telegram_id(user_id)
            try:r=await asyncio.to_thread(self._write,f'SELECT console.fn_comarb_ddjj_cancelar_telegram({actor},{int(parts[2])})::text;')
            except Exception:r=[]
            await query.answer('Cancelación pedida' if r==[True] else 'No pude confirmar la cancelación. Revisá Operaciones.')
            return True
        if len(parts)==2 and parts[1]=='uno':
            try:await asyncio.to_thread(self._scope().require_permission,user_id,'contribuyentes.editar')
            except Exception:await query.answer('Sin permiso para preparar archivos.');return True
        if len(parts)==4 and parts[1]=='actividad':
            if not state or state.stage!='actividad' or parts[2]!=state.nonce:
                await query.answer('Esa selección ya no está vigente.');return True
            code=parts[3]
            if code!='pendiente' and code not in {a['codigo'] for a in state.actividades}:
                await query.answer('Elegí una actividad de la lista.');return True
            await query.answer()
            return await self._encolar_actividad(adapter,state,key,state.periodo,code if code!='pendiente' else None,
                lambda text,**kw:adapter._bot.send_message(chat_id=chat_id,text=text,**kw))
        return await super().callback(adapter,query,data,chat_id,thread_id,user_id)

    async def text(self,adapter,msg):
        key=self._key(msg.chat_id,getattr(msg,'message_thread_id',None),str(msg.from_user.id))
        state=self.states.get(key)
        if state and state.stage=='actividad':
            await msg.reply_text('Elegí una actividad o «Dejar sin asignar» en el mensaje anterior. La elección vale sólo para este período.')
            return True
        return await super().text(adapter,msg)

    async def _submit_period(self,adapter,msg,state,key,selection):
        periods=list(selection.visible)
        if len(periods)!=1:await msg.reply_text('Ingresá un solo mes MM/AAAA.');return True
        period=periods[0];actor=FiscalScope._telegram_id(state.user_id);cid=int(state.contributor['id'])
        try:
            rows=await asyncio.to_thread(self._query,f"SELECT console.fn_sifere_actividades_opciones({actor},{cid},'{period}')::text;")
            acts=rows[0] if len(rows)==1 and isinstance(rows[0],list) else None
        except Exception:acts=None
        if acts is None:
            self.states.pop(key,None);await msg.reply_text('No pude verificar las actividades y tu acceso. No se inició la preparación.');return True
        if len(acts)>1:
            state.periodo=period;state.actividades=acts;state.stage='actividad'
            rows=[[InlineKeyboardButton(menu_label('📄',a['codigo']+' · '+a['descripcion']),callback_data=f"sa:actividad:{state.nonce}:{a['codigo']}")] for a in acts]
            rows.append([InlineKeyboardButton(menu_label('📝','Dejar sin asignar'),callback_data=f'sa:actividad:{state.nonce}:pendiente')])
            rows.append([InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'sa:cancel:{state.nonce}')])
            await msg.reply_text(f"{state.contributor['nombre']} · {period}\nHay varias actividades. Elegí una sólo si todas las ventas corresponden a ella. Si necesitás discriminarlas, dejalas sin asignar: el control las marcará para revisión. No se guarda un predeterminado permanente.",reply_markup=InlineKeyboardMarkup(rows));return True
        return await self._encolar_actividad(adapter,state,key,period,acts[0]['codigo'] if acts else None,msg.reply_text)

    async def _encolar_actividad(self,adapter,state,key,period,activity,send):
        self.states.pop(key,None)
        actor=FiscalScope._telegram_id(state.user_id);cid=int(state.contributor['id'])
        assert re.fullmatch(r'(0[1-9]|1[0-2])/20\d{2}',period)
        payload=json.dumps({'actividad':activity} if activity else {})
        try:rows=await asyncio.to_thread(self._write,f"SELECT console.fn_sifere_actividades_pedir({actor},{cid},'{period}','{payload}'::jsonb)::text;")
        except Exception:rows=[]
        r=rows[0] if len(rows)==1 and isinstance(rows[0],dict) else {}
        if r.get('estado')!='encolada':
            await send(self.rechazos.get(r.get('estado'),'No pude confirmar el pedido. Revisá Operaciones antes de repetirlo.'),reply_markup=self._back());return True
        jid=int(r['id_consulta']);head=TITLE+' · '+state.contributor['nombre']+' · '+period
        status=await send(head+'\nEn espera de la preparación. No se importa ni presenta ninguna DDJJ.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'sa:stop:{jid}')]]))
        self.watchers[jid]=asyncio.create_task(self._watch_preparacion(status,actor,jid,head))
        return True

    async def _watch_preparacion(self,message,actor,jid,head):
        last='';end=time.monotonic()+3600
        try:
            while time.monotonic()<end:
                await asyncio.sleep(3)
                try:r=await asyncio.to_thread(self._query,f'SELECT console.fn_comarb_ddjj_estado_telegram({actor},{jid})::text;')
                except Exception:continue
                if len(r)!=1 or not isinstance(r[0],dict):return
                terminal=r[0]['estado'] in ('terminada','error','cancelada')
                text=head+'\n'+('Preparación finalizada. Se verificará la entrega.' if terminal else (r[0].get('actual') or {}).get('progreso','En espera.'))
                if text!=last:
                    await message.edit_text(text,reply_markup=None if terminal else InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌','Cancelar'),callback_data=f'sa:stop:{jid}')]]));last=text
                if terminal:return
        finally:self.watchers.pop(jid,None)
