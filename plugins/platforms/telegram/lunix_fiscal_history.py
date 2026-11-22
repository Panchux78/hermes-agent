"""Registra CCMA/SCT en Operaciones con la procedencia real del pedido."""
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from plugins.platforms.telegram.bot_run_history import BotRunHistory
from plugins.platforms.telegram import documentos_contabot as docs
from plugins.platforms.telegram.execution_origin import environment

log = logging.getLogger(__name__)


def periodo_anual(value):
    for mask in (r'(20[0-9]{2})(?:-(?:0[1-9]|1[0-2])(?:-[0-9]{2})?)?',
                 r'(?:0[1-9]|1[0-2])/(20[0-9]{2})',r'(20[0-9]{2})0000'):
        if m := re.fullmatch(mask,value):
            return m.group(1)
    raise ValueError('periodo_anual_de_control_invalido')


def periodo_registrable(operation, params, *, automatic):
    start=params.get('period_from','')
    if automatic:
        return periodo_anual(start)
    if operation=='sct':
        end=params.get('period_until','')
        if params.get('period_mode')=='empty' and start==end=='':
            return None
        if not all(re.fullmatch(r'20[0-9]{2}(?:00|0[1-9]|1[0-2])[0-9]{2}', v) for v in (start,end)):
            raise ValueError('periodo_sct_no_acreditado')
        if start[:6]==end[:6] and start[4:6]!='00':
            return f'{start[:4]}-{start[4:6]}'
        return start[:4] if start[:4]==end[:4] else None
    end=params.get('period_to','')
    if not all(re.fullmatch(r'(?:0[1-9]|1[0-2])/20[0-9]{2}', v) for v in (start,end)):
        raise ValueError('periodo_ccma_no_acreditado')
    if start==end:
        return f'{start[3:]}-{start[:2]}'
    return start[3:] if start[3:]==end[3:] else None


@dataclass
class Delivery:
    output: Path | None = None
    relative: str | None = None
    delivered: bool = False
    code: str = 'sin_entrega_verificada'
    text: str = 'La consulta no acreditó una entrega.'

    def failure(self, code, text):
        if re.fullmatch(r'[a-z][a-z0-9_]{2,79}',code):
            self.code=code
            self.text=text

    def received(self, record, delivered):
        root=docs.raiz_clientes()
        path=docs.archivo_guardado(record['path'],root,record['ruta'],record.get('sha256'))
        self.output=path
        self.relative=str(path.relative_to(root))
        self.delivered=bool(delivered)


async def tracked(flow, operation, target, kwargs):
    origin=environment({})
    channel=origin['CONTABOT_ORIGEN_CANAL']
    automatic=(channel,origin['CONTABOT_ORIGEN_PROCESO'])==('automatico','control_diario_lunix')
    if channel!='telegram' and not automatic:
        return await target(**kwargs)
    contributor=kwargs.get('contributor_id')
    actor=kwargs.get('telegram_id')
    entry=origin['CONTABOT_ORIGEN_ENTRADA']
    if (type(contributor) is not int or contributor<=0 or str(actor)!=origin['CONTABOT_ORIGEN_ACTOR']
            or str(kwargs['chat_id'])!=str(actor) or not re.fullmatch(r'update:[0-9]+/(?:callback|mensaje:[0-9]+)',entry)):
        await flow.send(kwargs['chat_id'],'No se pudo registrar quién pidió la consulta. No se consultó ARCA.')
        return
    try:
        period=periodo_registrable(operation,kwargs,automatic=automatic)
    except (TypeError,ValueError):
        message=('No se acreditó el año del control diario. No se consultó ARCA.' if automatic else
                 'No se pudo registrar el período elegido. No se consultó ARCA.')
        await flow.send(kwargs['chat_id'],message)
        return
    delivery=Delivery()
    history=BotRunHistory()
    try:
        handle=await history.start(telegram_id=int(actor),operation=operation,key_material=operation+':'+entry,
                                   reference='control-diario-lunix' if automatic else operation,lease_seconds=600)
    except Exception:
        log.exception('fiscal_history_start_failed operation=%s',operation)
        await flow.send(kwargs['chat_id'],'No se pudo guardar la consulta en Operaciones. No se consultó ARCA.')
        return
    state='fallido'
    try:
        await target(**kwargs,qa_record=delivery)
        if delivery.delivered:
            state='completado'
            delivery.failure('archivo_entregado','Telegram confirmó la entrega; archivo guardado en Documentos.')
        elif delivery.output is not None:
            state='incompleto'
    except asyncio.CancelledError:
        state='cancelado'
        delivery.failure('cancelado_por_usuario','Consulta cancelada; no se repite.')
        raise
    finally:
        try:
            await history.finish_single(handle,state=state,reason_code=delivery.code,reason_text=delivery.text,effects=[],
                                        contributor_id=contributor,period=period,
                                        output=delivery.output,output_relative=delivery.relative)
        except Exception:
            log.exception('fiscal_history_finish_failed operation=%s',operation)
