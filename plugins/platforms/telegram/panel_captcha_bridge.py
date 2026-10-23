"""CAPTCHA de una consulta COMARB (panel o Telegram): bot existente, nonce, dueño y vencimiento."""
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time


def private_json(path, limit=8192):
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>limit:
            raise ValueError('captcha_artifact_invalid')
        return json.loads(stream.read(limit+1))


def publish(path,payload):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as stream:
        json.dump(payload,stream,separators=(',',':'))
        stream.flush();os.fsync(stream.fileno())


CAPTIONS={
    'ARCA':'Consulta COMARB. ARCA pide este CAPTCHA. Respondé a esta imagen con sus caracteres; vence en menos de dos minutos.',
    'Rentas Tucumán':'Consulta de Tucumán. Rentas Tucumán pide este código de seguridad. Respondé a esta imagen con sus caracteres; vence en menos de dos minutos.',
}


class PanelCaptchaBridge:
    def __init__(self,root=None):
        self.root=Path(root or Path(os.environ.get('CONTABOT_STATE_ROOT',str(Path.home()/'.local/state/contabot')))/'panel-captcha')

    def requests(self):
        if not self.root.exists(): return []
        info=self.root.lstat()
        if self.root.is_symlink() or self.root.resolve()!=self.root or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('captcha_root_invalid')
        result=[]
        for path in self.root.glob('*.request.json'):
            nonce=path.name.removesuffix('.request.json')
            if not re.fullmatch('[a-f0-9]{16}',nonce): continue
            request=private_json(path)
            # 'origen' es opcional: quién pide el CAPTCHA, de una lista cerrada.
            if not {'nonce','telegram_id','expires','captured_at'}<=set(request)<={'nonce','telegram_id','expires','captured_at','origen'} or request['nonce']!=nonce:
                raise ValueError('captcha_request_invalid')
            if request.get('origen','ARCA') not in CAPTIONS:
                raise ValueError('captcha_request_invalid')
            if type(request['telegram_id']) is not int or request['telegram_id']<=0:
                raise ValueError('captcha_recipient_invalid')
            if not all(type(request[key]) in (int,float) for key in ('captured_at','expires')):
                raise ValueError('captcha_expiry_invalid')
            if not request['captured_at']<=time.time()<request['expires']<=request['captured_at']+120:
                continue
            result.append(request)
        return result

    async def poll(self,bot):
        for request in await asyncio.to_thread(self.requests):
            nonce=request['nonce'];sent=self.root/(nonce+'.sent.json')
            if sent.exists() or request['expires']-time.time()<10: continue
            # Claim before sending: reconnects never replay a CAPTCHA image.
            publish(sent,{'nonce':nonce,'message_id':None})
            try:
                path=self.root/(nonce+'.png')
                with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
                    info=os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600 or not 0<info.st_size<=1000000:
                        raise ValueError('captcha_image_invalid')
                    image=stream.read(1000001)
                if not image.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('captcha_image_invalid')
                from telegram import ForceReply
                message=await bot.send_photo(chat_id=request['telegram_id'],photo=image,
                    caption=CAPTIONS[request.get('origen','ARCA')],
                    reply_markup=ForceReply(selective=True))
                publish(self.root/(nonce+'.delivered.json'),{'nonce':nonce,'message_id':message.message_id})
            except asyncio.CancelledError: raise
            except Exception:
                publish(self.root/(nonce+'.failed.json'),{'nonce':nonce,'code':'captcha_no_entregado'})

    async def text(self,bot,message):
        if getattr(message.chat,'type',None)!='private' or not message.from_user: return False
        reply=getattr(message,'reply_to_message',None)
        if reply is None: return False
        # Validate the private queue before resolving any reply.
        await asyncio.to_thread(self.requests)
        # Reply to the actual photo, not a different challenge or another user's text.
        for path in self.root.glob('*.delivered.json') if self.root.is_dir() else ():
            delivered=private_json(path);nonce=path.name.removesuffix('.delivered.json')
            if not re.fullmatch('[a-f0-9]{16}',nonce): continue
            if delivered.get('message_id')!=reply.message_id: continue
            request=private_json(self.root/(nonce+'.request.json'))
            if request['telegram_id']!=message.from_user.id or message.chat.id!=message.from_user.id:
                return False
            if (self.root/(nonce+'.closed.json')).exists() or request['expires']<=time.time():
                await bot.send_message(chat_id=message.chat.id,text='Ese CAPTCHA ya venció. La consulta se canceló sin cambiar nada; volvé a iniciarla.')
                return True
            solution=(message.text or '').strip()
            if not re.fullmatch('[A-Za-z0-9]{4,20}',solution):
                await bot.send_message(chat_id=message.chat.id,text='Respondé a la imagen sólo con los caracteres del CAPTCHA.')
                return True
            if (self.root/(nonce+'.answered.json')).exists():
                await bot.send_message(chat_id=message.chat.id,text='La respuesta a ese CAPTCHA ya fue recibida.')
                return True
            try:
                publish(self.root/(nonce+'.answered.json'),{'nonce':nonce})
                publish(self.root/(nonce+'.answer.json'),{'nonce':nonce,'solution':solution})
            except FileExistsError:
                await bot.send_message(chat_id=message.chat.id,text='La respuesta a ese CAPTCHA ya fue recibida.')
                return True
            await bot.send_message(chat_id=message.chat.id,text='CAPTCHA recibido. La consulta continúa.')
            return True
        return False

    async def run(self,adapter):
        while True:
            try:
                if adapter._bot is not None: await self.poll(adapter._bot)
            except asyncio.CancelledError: raise
            except Exception:
                # Nunca registrar datos de la solicitud ni respuestas del usuario.
                import logging
                logging.getLogger(__name__).warning('Puente CAPTCHA del panel: diagnóstico técnico no disponible')
            await asyncio.sleep(.5)
