import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from plugins.platforms.telegram.panel_captcha_bridge import PanelCaptchaBridge, publish


def challenge(tmp_path,**extra):
    tmp_path.chmod(0o700)
    nonce='a'*16;now=time.time()
    publish(tmp_path/(nonce+'.request.json'),dict(nonce=nonce,telegram_id=7,captured_at=now,expires=now+120,**extra))
    image=tmp_path/(nonce+'.png');image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic');image.chmod(0o600)
    bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=42)),send_message=AsyncMock())
    return PanelCaptchaBridge(tmp_path),bot,nonce


def test_new_image_sent_once_and_matching_owner_answer_only(tmp_path):
    async def scenario():
        bridge,bot,nonce=challenge(tmp_path)
        await bridge.poll(bot);await bridge.poll(bot)
        bot.send_photo.assert_awaited_once()
        def message(uid=7,reply=42):
            return SimpleNamespace(chat=SimpleNamespace(type='private',id=uid),from_user=SimpleNamespace(id=uid),reply_to_message=SimpleNamespace(message_id=reply),text='AB12')
        assert not await bridge.text(bot,message(uid=8))
        assert not await bridge.text(bot,message(reply=43))
        assert not (tmp_path/(nonce+'.answer.json')).exists()
        assert await bridge.text(bot,message())
        assert json.loads((tmp_path/(nonce+'.answer.json')).read_text())==dict(nonce=nonce,solution='AB12')
        assert (tmp_path/(nonce+'.answer.json')).stat().st_mode&0o777==0o600
        (tmp_path/(nonce+'.answer.json')).unlink()
        assert await bridge.text(bot,message())
        assert not (tmp_path/(nonce+'.answer.json')).exists()
    asyncio.run(scenario())


def test_expired_image_never_sent(tmp_path):
    async def scenario():
        bridge,bot,nonce=challenge(tmp_path)
        path=tmp_path/(nonce+'.request.json')
        data=json.loads(path.read_text());data['captured_at']=time.time()-125;data['expires']=time.time()-5
        path.write_text(json.dumps(data))
        await bridge.poll(bot)
        bot.send_photo.assert_not_awaited()
    asyncio.run(scenario())


def test_origen_rentas_tucuman_cambia_solo_el_texto(tmp_path):
    async def scenario():
        bridge,bot,nonce=challenge(tmp_path,origen='Rentas Tucumán')
        await bridge.poll(bot)
        assert 'Rentas Tucumán pide' in bot.send_photo.await_args.kwargs['caption']
    asyncio.run(scenario())


def test_origen_desconocido_se_rechaza(tmp_path):
    bridge,bot,nonce=challenge(tmp_path,origen='Otro')
    try:
        bridge.requests()
    except ValueError as exc:
        assert str(exc)=='captcha_request_invalid'
    else:
        raise AssertionError('origen desconocido aceptado')
