"""Registra en Operaciones los dos ejecutores directos sólo durante el control."""
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


@dataclass
class Delivery:
    output: Path | None = None
    relative: str | None = None
    delivered: bool = False

    def received(self, record, delivered):
        root=docs.raiz_clientes()
        path=docs.archivo_guardado(record['path'],root,record['ruta'],record.get('sha256'))
        self.output=path
        self.relative=str(path.relative_to(root))
        self.delivered=bool(delivered)


async def tracked(flow, operation, target, kwargs):
    origin=environment({})
    if (origin['CONTABOT_ORIGEN_CANAL'],origin['CONTABOT_ORIGEN_PROCESO'])!=('automatico','control_diario_lunix'):
        return await target(**kwargs)
    contributor=kwargs.get('contributor_id')
    actor=kwargs.get('telegram_id')
    if (not isinstance(contributor,int) or contributor<=0
            or str(actor)!=origin['CONTABOT_ORIGEN_ACTOR'] or str(kwargs['chat_id'])!=str(actor)):
        await flow.send(kwargs['chat_id'],'No se acreditó el caso del control diario. No se consultó ARCA.')
        return
    try:
        period=periodo_anual(kwargs.get('period_from',''))
    except (TypeError,ValueError):
        await flow.send(kwargs['chat_id'],'No se acreditó el año del control diario. No se consultó ARCA.')
        return
    delivery=Delivery()
    history=BotRunHistory()
    entry=origin['CONTABOT_ORIGEN_ENTRADA']
    try:
        handle=await history.start(telegram_id=int(actor),operation=operation,key_material=operation+':'+entry,
                                    reference='control-diario-lunix',lease_seconds=600)
    except Exception:
        log.exception('lunix_fiscal_history_start_failed operation=%s',operation)
        await flow.send(kwargs['chat_id'],'No se pudo registrar el control diario. No se consultó ARCA.')
        return
    state,code,text='fallido','sin_entrega_verificada','La consulta no acreditó una entrega.'
    try:
        await target(**kwargs,qa_record=delivery)
        if delivery.delivered:
            state,code,text='completado','archivo_entregado','Telegram confirmó la entrega; archivo guardado en Documentos.'
    except asyncio.CancelledError:
        state,code,text='cancelado','cancelado_por_usuario','Control cancelado; no se repite.'
        raise
    finally:
        try:
            await history.finish_single(handle,state=state,reason_code=code,reason_text=text,effects=[],
                                        contributor_id=contributor,period=period,
                                        output=delivery.output,output_relative=delivery.relative)
        except Exception:
            log.exception('lunix_fiscal_history_finish_failed operation=%s',operation)
