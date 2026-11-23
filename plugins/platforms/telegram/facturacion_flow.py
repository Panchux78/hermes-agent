"""Revisión de la planilla de facturación; no entrega archivos al modelo ni emite."""
import asyncio
from io import BytesIO
import logging
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow
from plugins.platforms.telegram.contabot_compartido import cargar
from plugins.platforms.telegram.fiscal_scope import AccountNotLinked, FiscalScope, _literal

log=logging.getLogger(__name__)
MAX_FILE=8*1024*1024


class FacturacionFlow:
    def __init__(self,query_connection):
        self.query_connection=query_connection
        self.pending={}
        self.busy=set()

    def _query(self,sql):
        return ComarbDeduccionesFlow._run(sql,self.query_connection)

    def _actor(self,user):
        scope=FiscalScope(self._query,'ARCA',binding='mis_retenciones')
        scope.require_actor(user)
        scope.require_permission(user,'operaciones.ejecutar')
        return scope._telegram_id(user)

    def _scope(self,actor,cuits):
        # Exact CUIT search (digits validated by the common motor); reuse actor/carriage guards.
        values=','.join('('+_literal(c)+')' for c in cuits)
        rows=self._query(f"""SELECT json_build_object('cuit',q.doc,'nombre',s.nombre_legal,
          'id_estudio',s.id_estudio,'acceso_verificado',b.id_acceso IS NOT NULL)::text
          FROM (VALUES {values}) q(doc)
          CROSS JOIN LATERAL console.fn_buscar_contribuyente_fiscal({actor},'ARCA',q.doc,NULL) s
          LEFT JOIN LATERAL console.fn_mis_retenciones_binding_telegram({actor},s.id_contribuyente) b ON TRUE
          WHERE s.cuit=q.doc;""")
        safe={}
        for row in rows:
            if row['cuit'] in safe:
                raise RuntimeError('alcance_ambiguo')
            safe[row['cuit']]=row
        return safe

    @staticmethod
    def _menu(active=False):
        options=[[InlineKeyboardButton('Descargar modelo',callback_data='fe:modelo')]]
        options.append([InlineKeyboardButton('Cancelar',callback_data='fe:cancel')] if active
                       else [InlineKeyboardButton('‹ Preparar',callback_data='om:arca_preparar')])
        return InlineKeyboardMarkup(options)

    def cancel(self,chat,user):
        self.pending.pop((str(chat),str(user)),None)

    async def callback(self,query,data,cb):
        chat=str(cb['chat_id']); user=str(query.from_user.id)
        if str(cb['chat_type']).lower()!='private' or chat!=user:
            await query.answer('Usá el chat privado con ContaBot.')
            return
        try:
            await asyncio.to_thread(self._actor,user)
            shared=cargar('contabot_facturacion')
        except (AccountNotLinked,PermissionError):
            await query.answer('Tu cuenta no tiene permiso para revisar comprobantes.')
            return
        except Exception:
            log.exception('Facturación: no disponible al abrir')
            await query.answer('La revisión de facturación no está disponible.')
            return
        key=(chat,user)
        if data=='fe:cancel':
            self.cancel(chat,user)
            await query.answer()
            await query.edit_message_text('Revisión cancelada. No se emitió nada.',reply_markup=self._menu())
        elif data=='fe:modelo':
            await query.answer()
            model=await asyncio.to_thread(shared.modelo)
            if str(query.message.chat_id)==chat:
                await query.message.reply_document(InputFile(BytesIO(model),filename='modelo-facturacion.xlsx'))
        elif data=='fe:start':
            if key in self.busy:
                await query.answer('Ya estás revisando un archivo.')
                return
            now=monotonic()
            self.pending={k:v for k,v in self.pending.items() if now<v}
            self.pending[key]=now+300
            await query.answer()
            await query.edit_message_text('Facturación electrónica · Revisar Excel\n\n'
                'Adjuntá el Excel con los comprobantes y sus emisores y receptores. '
                'Esta revisión no emite facturas ni solicita CAE.',reply_markup=self._menu(True))
        else:
            await query.answer()

    async def document(self,msg):
        user=getattr(msg,'from_user',None)
        if user is None or str(msg.chat_id)!=str(user.id):
            return False
        key=(str(msg.chat_id),str(user.id))
        if key in self.busy:
            await msg.reply_text('Hay una revisión en curso. Esperá el resultado.')
            return True
        deadline=self.pending.get(key)
        if deadline is None:
            return False
        if monotonic()>=deadline:
            self.pending.pop(key,None)
            await msg.reply_text('Venció el pedido. Volvé a abrir Facturación electrónica.',reply_markup=self._menu())
            return True
        doc=msg.document
        if (not (doc.file_name or '').casefold().endswith('.xlsx') or
                not 0<(doc.file_size or 0)<=MAX_FILE):
            await msg.reply_text('Adjuntá un Excel (.xlsx) de hasta 8 MB.',reply_markup=self._menu(True))
            return True
        self.busy.add(key)
        try:
            actor=await asyncio.to_thread(self._actor,user.id)
            f=await doc.get_file()
            raw=bytes(await f.download_as_bytearray(read_timeout=20))
            shared=cargar('contabot_facturacion')
            result=await asyncio.to_thread(shared.revisar,raw,lambda ids:self._scope(actor,ids))
            if self.pending.get(key)==deadline:
                await asyncio.to_thread(self._actor,user.id)
                if self.pending.get(key)!=deadline:
                    return True
                output=await asyncio.to_thread(shared.control_excel,result)
                if self.pending.get(key)!=deadline:
                    return True
                checked=sum(r['estado']=='por_verificar' for r in result['comprobantes'])
                blocked=len(result['comprobantes'])-checked
                obs=len({r['fila'] for r in result['observaciones']})
                await msg.reply_document(InputFile(BytesIO(output),filename='control-facturacion.xlsx'),
                    caption=f'Facturación electrónica · Revisión\n{checked} para verificar en ARCA · {blocked} no disponibles · {obs} {"fila" if obs==1 else "filas"} para corregir.\nNo se emitió nada.',
                    reply_markup=self._menu())
                self.pending.pop(key,None)
        except ValueError as exc:
            await msg.reply_text(str(exc),reply_markup=self._menu(True))
        except (AccountNotLinked,PermissionError):
            self.pending.pop(key,None)
            await msg.reply_text('Tu cuenta no tiene permiso para revisar comprobantes.')
        except Exception as exc:
            log.warning('Facturación: no se pudo revisar o entregar clase=%s',type(exc).__name__)
            await msg.reply_text('No pude completar la revisión. No se emitió nada.',reply_markup=self._menu(True))
        finally:
            self.busy.discard(key)
        return True
