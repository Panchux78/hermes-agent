"""Facturación de la planilla exclusivamente en el chat privado autenticado."""
import asyncio
from hashlib import sha256
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
from time import monotonic

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow
from plugins.platforms.telegram.contabot_access import catalog_connection
from plugins.platforms.telegram.contabot_compartido import cargar
from plugins.platforms.telegram.execution_origin import environment as with_origin
from plugins.platforms.telegram.execution_origin import origen_del_mensaje, origen_callback_confirmado
from plugins.platforms.telegram.fiscal_scope import AccountNotLinked, FiscalScope

log=logging.getLogger(__name__)
MAX_FILE=8*1024*1024


class FacturacionFlow:
    def __init__(self,query_connection):
        self.query_connection=query_connection
        self.pending={}
        self.busy=set()
        self.previews={}
        self.jobs={}
        self.challenges={}
        self.challenge_messages={}
        self.confirmaciones={}

    def _query(self,sql):
        return ComarbDeduccionesFlow._run(sql,self.query_connection)

    def _actor(self,user):
        scope=FiscalScope(self._query,'ARCA',binding='mis_retenciones')
        scope.require_actor(user)
        scope.require_permission(user,'operaciones.ejecutar')
        return scope._telegram_id(user)

    def _scope(self,actor,cuits):
        # Exact CUIT search (digits validated by the common motor); reuse actor/carriage guards.
        if not cuits or len(cuits)>2000 or not all(isinstance(c,str) and re.fullmatch(r'[0-9]{11}',c) for c in cuits):
            raise ValueError('No pude verificar los CUIT de la planilla.')
        values=','.join(f"'{c}'" for c in cuits)
        rows=self._query(f"""SELECT json_build_object('cuit',q.doc,'nombre',s.nombre_legal,
          'id_estudio',s.id_estudio,'id_contribuyente',s.id_contribuyente,'acceso_verificado',b.id_acceso IS NOT NULL)::text
          FROM unnest(ARRAY[{values}]::text[]) q(doc)
          CROSS JOIN LATERAL console.fn_buscar_contribuyente_fiscal({actor},'ARCA',q.doc,NULL) s
          LEFT JOIN LATERAL console.fn_mis_retenciones_binding_telegram({actor},s.id_contribuyente) b ON TRUE
          WHERE s.cuit=q.doc;""")
        safe={}
        for row in rows:
            if row['cuit'] in safe:
                raise RuntimeError('alcance_ambiguo')
            safe[row['cuit']]=row
        return safe

    def _job(self,actor,jid,action,data=None,*,origen_mensaje=None):
        assert type(actor) is int and actor>0 and (jid is None or type(jid) is int and jid>0)
        assert action in {'crear','estado','imagen','captcha','cancelar'}
        raw=json.dumps(data or {},ensure_ascii=True).replace("'","''")
        sql=('SET standard_conforming_strings=on; SELECT console.fn_facturacion_previa_usuario('
             f"NULL,{actor},{jid if jid else 'NULL'},'{action}','{raw}'::jsonb)::text;")
        connection=catalog_connection if action in {'crear','captcha','cancelar'} else self.query_connection
        command,environment=connection()
        if action in {'crear','captcha','cancelar'}:
            environment=with_origin(environment,origen_mensaje=origen_mensaje)
        result=subprocess.run([*command,'-XqAt','-v','ON_ERROR_STOP=1'],input=sql,env=environment,
                              text=True,capture_output=True,timeout=15)
        if result.returncode:
            raise RuntimeError('facturacion_cola_no_disponible')
        return json.loads(result.stdout.strip() or 'null')

    def _emision(self,actor,jid,action,data=None,*,origin=None):
        assert type(actor) is int and actor>0 and type(jid) is int and jid>0
        assert action in {'emitir','estado','archivo'}
        if action=='emitir' and origin is None:
            return None
        raw=json.dumps(data or {},ensure_ascii=True).replace("'","''")
        command,env=(catalog_connection if action=='emitir' else self.query_connection)()
        if action=='emitir':
            env=with_origin(env,origen_mensaje=origin)
        sql=("SET standard_conforming_strings=on; SELECT console.fn_facturacion_emision_usuario("
             f"NULL,{actor},{jid},'{action}','{raw}'::jsonb)::text;")
        result=subprocess.run([*command,'-XqAt','-v','ON_ERROR_STOP=1'],input=sql,env=env,text=True,
                              capture_output=True,timeout=15)
        if result.returncode:
            raise RuntimeError('facturacion_cola_no_disponible')
        return json.loads(result.stdout.strip() or 'null')

    @staticmethod
    def _pdf(meta):
        root=Path(os.environ.get('CONTABOT_CLIENTES_ROOT',str(Path.home()/'clientes'))).resolve(strict=True)
        part=Path(meta['ruta'])
        if part.is_absolute() or '..' in part.parts or part.name!=meta['nombre'] or not part.name.endswith('.pdf'):
            raise ValueError('pdf_factura_no_valido')
        path=(root/part).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size!=meta['bytes'] or not 200<meta['bytes']<45*1024*1024:
            raise ValueError('pdf_factura_no_valido')
        data=path.read_bytes()
        if not data.startswith(b'%PDF-') or sha256(data).hexdigest()!=meta['sha256']:
            raise ValueError('pdf_factura_no_valido')
        return InputFile(BytesIO(data),filename=part.name)

    @staticmethod
    def _menu(active=False,queued=None,preview=None):
        options=[[InlineKeyboardButton('Descargar modelo',callback_data='fe:modelo')]]
        if preview:
            options.append([InlineKeyboardButton('Verificar en ARCA',callback_data=f'fe:confirm:{preview}')])
        options.append([InlineKeyboardButton('Cancelar',callback_data=f'fe:cancel:{queued}' if queued else 'fe:cancel')] if active
                       else [InlineKeyboardButton('‹ Preparar',callback_data='om:arca_preparar')])
        return InlineKeyboardMarkup(options)

    def cancel(self,chat,user):
        key=(str(chat),str(user));self.pending.pop(key,None);self.previews.pop(key,None)

    async def callback(self,query,data,cb):
        chat=str(cb['chat_id']); user=str(query.from_user.id)
        if str(cb['chat_type']).lower()!='private' or chat!=user:
            await query.answer('Usá el chat privado con ContaBot.')
            return
        try:
            actor=await asyncio.to_thread(self._actor,user)
            shared=cargar('contabot_facturacion')
        except (AccountNotLinked,PermissionError):
            await query.answer('Tu cuenta no tiene permiso para revisar comprobantes.')
            return
        except Exception:
            log.exception('Facturación: no disponible al abrir')
            await query.answer('La revisión de facturación no está disponible.')
            return
        key=(chat,user)
        match=re.fullmatch(r'fe:emit:(\d{1,18}):([a-zA-Z0-9_-]{10,24})',data)
        if match:
            jid=int(match[1])
            current=self.confirmaciones.get(key)
            if (not current or current[:2]!=(jid,match[2]) or current[2]<=monotonic()
                    or query.message.message_id!=current[4] or key in self.busy or key in self.jobs):
                await query.answer('Esta confirmación no está vigente. Revisá Operaciones.')
                return
            origin=origen_callback_confirmado(actor)
            if not origin:
                await query.answer('No se puede emitir desde una prueba ni desde otro chat.')
                return
            self.confirmaciones.pop(key,None)
            self.busy.add(key)
            try:
                result=await asyncio.to_thread(self._emision,actor,jid,'emitir',
                                               {'confirmo':True,'filas':current[3]},origin=origin)
                if not result or not result.get('id_emision'):
                    await query.answer('No pude confirmar el inicio. Revisá Operaciones.')
                    return
                emision_id=int(result['id_emision'])
                await query.answer('Confirmación recibida. Sigo en ARCA.')
                task=asyncio.create_task(self._wait_job(query.message,key,actor,emision_id,shared,None,emision=jid),
                                         name=f'facturacion-emision-{emision_id}')
                self.jobs[key]=(emision_id,task)
            except Exception as ex:
                log.warning('Facturación: no se confirmó el inicio; clase=%s',type(ex).__name__)
                await query.answer('No pude confirmar el inicio. Revisá Operaciones antes de volver a emitir.')
            finally:
                self.busy.discard(key)
            return
        if data.startswith('fe:confirm:'):
            preview=self.previews.get(key)
            if not preview or preview[1]<=monotonic() or data!=f'fe:confirm:{preview[0]}':
                await query.answer('Esta vista previa ya no está vigente. Volvé a cargar el Excel.')
                return
            if key in self.busy or key in self.jobs:
                await query.answer('Ya hay una verificación en curso.')
                return
            self.busy.add(key)
            _,_,result,candidates,origen=preview
            try:
                jid=await asyncio.to_thread(shared.solicitar_previa,result,candidates,
                    lambda cases:self._job(actor,None,'crear',{'casos':cases},
                                           origen_mensaje=origen))
                self.cancel(*key)
                await query.answer('Verificación iniciada. No se emite nada.')
                task=asyncio.create_task(self._wait_job(query.message,key,actor,jid,shared,result),
                                         name=f'facturacion-previa-{jid}')
                self.jobs[key]=(jid,task)
            except Exception:
                # No repetir por una respuesta ambigua: revisar Operaciones.
                self.cancel(*key)
                await query.answer('No pude confirmar el inicio. Revisá Operaciones. No se emitió nada.')
            finally:
                self.busy.discard(key)
            return
        if data.startswith('fe:cancel:'):
            job=self.jobs.get(key)
            if job and data==f'fe:cancel:{job[0]}':
                try:cancelled=await asyncio.to_thread(self._job,int(user),job[0],'cancelar')
                except Exception:cancelled=None
                await query.answer('Cancelación solicitada. No se emitió nada.' if cancelled else 'No pude cancelar. Revisá Operaciones.')
            else:
                await query.answer('Esta verificación ya no está en curso.')
            return
        if data=='fe:cancel':
            self.cancel(chat,user)
            await query.answer()
            await query.edit_message_text('Revisión cancelada. No se emitió nada.',reply_markup=self._menu())
        elif data=='fe:modelo':
            await query.answer()
            model=await asyncio.to_thread(shared.modelo)
            if str(query.message.chat_id)==chat:
                await query.message.reply_document(InputFile(BytesIO(model),filename='modelo-facturacion.xlsx'))
        elif data in ('fe:start','fe:prepare'):
            if key in self.busy or key in self.jobs:
                await query.answer('Ya estás revisando un archivo.')
                return
            now=monotonic()
            self.pending={k:v for k,v in self.pending.items() if now<v}
            self.previews={k:v for k,v in self.previews.items() if now<v[1]}
            self.cancel(*key)
            self.pending[key]=now+300
            await query.answer()
            await query.edit_message_text('Facturación electrónica · Revisar Excel\n\n'
                'Adjuntá el Excel. Primero te mando una vista previa con neto, IVA y total, sin entrar a ARCA. '
                'Después podés verificar hasta el resumen. No se emite nada.',reply_markup=self._menu(True))
        else:
            await query.answer()

    async def document(self,msg):
        user=getattr(msg,'from_user',None)
        if user is None or str(msg.chat_id)!=str(user.id):
            return False
        key=(str(msg.chat_id),str(user.id))
        if key in self.busy or key in self.jobs:
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
            result,candidates=await asyncio.to_thread(shared.revisar,raw,lambda ids:self._scope(actor,ids),
                                                       preparar=True)
            if self.pending.get(key)==deadline:
                await asyncio.to_thread(self._actor,user.id)
                if self.pending.get(key)!=deadline:
                    return True
                self.cancel(*key)
                nonce=secrets.token_urlsafe(9) if 0<len(candidates)<=shared.MAX_PREVIA else None
                if nonce:
                    self.previews[key]=(nonce,monotonic()+300,result,candidates,origen_del_mensaje(actor))
                try:
                    await self._result(msg,shared,result,preview=nonce,
                                       excede=len(candidates)>shared.MAX_PREVIA)
                except Exception:
                    self.previews.pop(key,None)
                    raise
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

    async def _result(self,msg,shared,result,operation=None,*,preview=None,excede=False):
        output=await asyncio.to_thread(shared.control_excel,result)
        pending=sum(r['estado']=='por_verificar' for r in result['comprobantes'])
        ok=sum(r['estado']=='resumen_arca' for r in result['comprobantes'])
        blocked=len(result['comprobantes'])-pending-ok
        obs=len({r['fila'] for r in result['observaciones']})
        summary=f'{ok} con resumen verificado' if operation else f'{pending} para verificar en ARCA'
        ref=f'\nReferencia en Operaciones: {operation}.' if operation else ''
        titulo='Revisión' if operation else 'Vista previa · Neto, IVA y total en el Excel'
        if excede:
            ref+='\nPara verificar en ARCA, cargá hasta 30 comprobantes por Excel.'
        await msg.reply_document(InputFile(BytesIO(output),filename='control-facturacion.xlsx'),
            caption=f'Facturación electrónica · {titulo}\n{summary} · {blocked} no disponibles · {obs} {"fila" if obs==1 else "filas"} para corregir.\nNo se emitió nada.{ref}',
            reply_markup=self._menu(active=bool(preview),preview=preview))

    async def _ofrecer_emision(self,msg,key,actor,jid,revision):
        estado=await asyncio.to_thread(self._emision,actor,jid,'estado')
        filas=[r['fila'] for r in (estado or {}).get('filas',[]) if r['estado']=='verificada']
        if not filas or (estado or {}).get('id_emision'):
            return
        datos=[r for r in revision['comprobantes'] if r['fila'] in filas and r['estado']=='resumen_arca']
        if len(datos)!=len(filas):
            return
        nonce=secrets.token_urlsafe(10)
        info='\n'.join(f'Fila {r["fila"]} · Receptor {r["receptor"]} · Total {r["total"]} {r["moneda"]}' for r in datos)
        sent=await msg.reply_text(
            f'Revisá antes de emitir:\n{info}\n\nSi tocás Emitir, ContaBot va a solicitar el CAE a ARCA.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Emitir',callback_data=f'fe:emit:{jid}:{nonce}')]]))
        self.confirmaciones={k:v for k,v in self.confirmaciones.items() if v[2]>monotonic()}
        self.confirmaciones[key]=(jid,nonce,monotonic()+7200,filas,sent.message_id)

    async def _entregar_emision(self,msg,actor,previa):
        result=await asyncio.to_thread(self._emision,actor,previa,'estado')
        if not result:
            raise RuntimeError('resultado_emision_no_visible')
        lines=[]
        for row in result['filas']:
            await asyncio.to_thread(self._actor,actor)
            line=f'Fila {row["fila"]}: '+(row.get('motivo') or 'Confirmación pendiente. Revisá Operaciones.')
            if row.get('numero') and row.get('punto'):
                line+=f' {row["punto"]:05d}-{row["numero"]:08d} · CAE {row["cae"]}.'
            if row.get('id_archivo'):
                try:
                    meta=await asyncio.to_thread(self._emision,actor,previa,'archivo',{'fila':row['fila']})
                    pdf=await asyncio.to_thread(self._pdf,meta)
                    await msg.reply_document(pdf,caption=f'Factura oficial · Fila {row["fila"]}')
                except Exception as ex:
                    log.warning('Facturación: no se adjuntó PDF; clase=%s',type(ex).__name__)
                    line+=' No pude adjuntarlo acá; descargalo en Documentos.'
            lines.append(line)
        for i in range(0,len(lines),8):
            await msg.reply_text('\n'.join(lines[i:i+8])+f'\nReferencia en Operaciones: {result["id_corrida"]}.')

    async def _wait_job(self,msg,key,actor,jid,shared,revision,*,emision=None):
        last=None;sent=set();until=monotonic()+24*60
        buttons=None if emision else self._menu(True,jid)
        espera='No pude mostrar el resultado. Revisá Operaciones antes de volver a emitir.' if emision else 'No pude mostrar el resultado. Revisá Operaciones antes de reintentar. No se emitió nada.'
        try:
            progress=await msg.reply_text('Facturación electrónica · Esperando ARCA…',reply_markup=buttons)
            while monotonic()<until:
                status=await asyncio.to_thread(self._job,actor,jid,'estado')
                if not status:raise RuntimeError('facturacion_no_visible')
                if status['progreso']!=last:
                    last=status['progreso']
                    try:await progress.edit_text(last,reply_markup=buttons)
                    except Exception as ex:log.warning('Facturación: no se editó el avance; clase=%s',type(ex).__name__)
                nonce=status.get('captcha')
                if nonce and nonce not in sent and re.fullmatch('[a-f0-9]{16}',nonce):
                    image=await asyncio.to_thread(self._job,actor,jid,'imagen',{'nonce':nonce})
                    if image and image.get('imagen'):
                        shot=bytes.fromhex(image['imagen'])
                        if len(shot)<=1_000_000 and shot.startswith(b'\x89PNG\r\n\x1a\n'):
                            reply=await msg.reply_photo(InputFile(BytesIO(shot),filename='captcha-arca.png'),
                                caption='ARCA · Facturación electrónica. Respondé a esta imagen con sus caracteres.',reply_markup=ForceReply(selective=True))
                            self.challenges[key]=(jid,nonce,reply.message_id,monotonic()+105)
                            self.challenge_messages={k:v for k,v in self.challenge_messages.items() if v>monotonic()}
                            self.challenge_messages[(*key,reply.message_id)]=monotonic()+86_400
                            sent.add(nonce)
                if status['estado'] not in ('pendiente','consultando'):
                    await asyncio.to_thread(self._actor,actor)
                    if emision:
                        await self._entregar_emision(msg,actor,emision)
                    else:
                        final=shared.aplicar_previa(revision,status)
                        await self._result(msg,shared,final,status.get('id_corrida'))
                        if any(row['estado']=='resumen_arca' for row in final['comprobantes']):
                            await self._ofrecer_emision(msg,key,actor,jid,final)
                    return
                await asyncio.sleep(1.5)
            await msg.reply_text(espera)
        except Exception as ex:
            log.warning('Facturación: no se pudo entregar el seguimiento; clase=%s',type(ex).__name__)
            await msg.reply_text(espera)
        finally:
            self.jobs.pop(key,None);self.challenges.pop(key,None)

    async def captcha_text(self,msg):
        user=getattr(msg,'from_user',None)
        if not user or str(getattr(msg,'chat_id',''))!=str(user.id):return False
        key=(str(msg.chat_id),str(user.id));challenge=self.challenges.get(key)
        replied=getattr(msg,'reply_to_message',None)
        replied_id=getattr(replied,'message_id',None)
        if not challenge or replied_id!=challenge[2]:
            if self.challenge_messages.get((*key,replied_id),0)>monotonic() or (
               getattr(getattr(replied,'from_user',None),'is_bot',False) and
               getattr(replied,'caption','')=='ARCA · Facturación electrónica. Respondé a esta imagen con sus caracteres.'):
                try:await msg.delete()
                except Exception:pass
                await msg.reply_text('Ese CAPTCHA ya no está vigente. No se envió a ARCA.')
                return True
            return False
        jid,nonce,_,expiry=challenge
        solution=(msg.text or '').strip()
        if not re.fullmatch('[A-Za-z0-9]{4,20}',solution):
            await msg.reply_text('Usá sólo los caracteres de la imagen.');return True
        try:
            ok=monotonic()<expiry and await asyncio.to_thread(self._job,int(user.id),jid,'captcha',{'nonce':nonce,'solucion':solution})
        except Exception:ok=False
        self.challenges.pop(key,None)
        try:await msg.delete()
        except Exception:pass
        await msg.reply_text('Recibí el CAPTCHA. Sigo verificando.' if ok else 'El CAPTCHA venció o cambió. Usá la imagen vigente.')
        return True
