"""#191: ARCA › Preparar › Preparar período nuevo › Elegir clientes (lote = tildados × un período).

Mismo selector, período, avance y cancelación que Deducciones de IIBB; la cola es la fiscal común
(operación portal_iva_generar) y la atiende el worker de Portal IVA con el ejecutor de un cliente.
«Un cliente» sigue siendo el flujo de siempre (pi:generar).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow, RECHAZOS
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.portal_iva_flow import _IVA_INELIGIBLE

logger = logging.getLogger(__name__)
TITULO = '🧾 Preparar período nuevo'
CANCELADA_ANTES = 'No se preparó: cancelaste la corrida antes de llegar a este contribuyente.'


class PrepararLoteFlow(ComarbDeduccionesFlow):
    callback_prefix = 'pp'
    title = TITULO
    organismo = 'ARCA'
    back_page = 'om:arca_preparar_nuevo'
    operacion = 'portal_iva_generar'
    lote_funcion = 'preparar'
    espera = 'En espera de Preparar período nuevo.'
    rechazos = {**RECHAZOS,
        'periodo_invalido': 'Elegí un período MM/AAAA que no sea posterior al mes actual.',
        'lote_invalido': 'Elegí entre 1 y 100 clientes.',
        'sin_telegram': 'Tu usuario no tiene Telegram vinculado: el CAPTCHA de ARCA llega por acá.',
        'varios_estudios': 'Elegí clientes de un mismo estudio.',
        'ocupado': 'Ya hay una consulta fiscal en curso en tu estudio. Esperá que termine o cancelala; no se inició otra.',
        'sin_permiso': 'Tu usuario no tiene permiso para preparar períodos. No se ingresó a ARCA.'}

    def _lote_titulo(self) -> str:
        return (f'{TITULO} · Elegir clientes\nPrepara el período en Portal IVA (crea el borrador si no existe e importa '
                'los comprobantes de ARCA) y te manda los libros. Sólo aparecen los Responsables Inscriptos '
                'con un acceso ARCA comprobado.')

    def _lote_sql(self, actor: int, state, period: str) -> str:
        ids = ','.join(str(int(i['id'])) for i in state.lote.elegidos)
        return f"SELECT console.fn_portal_iva_preparar_lote_telegram({actor},'{period}',ARRAY[{ids}]::bigint[])::text;"

    def _no_incluidos(self, filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**f, 'motivo': f['motivo'] if str(f.get('motivo','')).startswith('Completá Ficha › Impuestos › IVA:') else _IVA_INELIGIBLE.get(f.get('motivo'), 'No se puede preparar con tu acceso actual.')} for f in filas]

    async def _respuesta(self,send,state,period,answer):
        if answer and answer.get('estado')=='sin_configuracion':
            from plugins.platforms.telegram.iva_configuracion import dividir
            text='No se inició ARCA. Completá las fichas antes de preparar:\n'+'\n'.join(f"• {r['nombre']}: {r['motivo']}" for r in self._no_incluidos(answer.get('no_incluidos') or []))
            for chunk in dividir(text):await send(chunk,reply_markup=self._back())
            return True
        return await super()._respuesta(send,state,period,answer)

    async def _encolar(self,adapter,chat_id,state,period,send):
        from plugins.platforms.telegram.iva_configuracion import consulta,mensajes_lote
        try:
            ids=[int(i['id']) for i in state.lote.elegidos]
            rows=await asyncio.to_thread(self._write,consulta(int(state.user_id),ids,period[3:]+'-'+period[:2]))
            if len(rows)!=1 or not isinstance(rows[0],list):raise ValueError('preflight_no_verificado')
            # Compactar por límite de Telegram, no un mensaje por cliente.
            for text in mensajes_lote(rows[0],period):await send(text)
        except Exception:
            await send('No se pudo comprobar la configuración de IVA. No se inició ARCA.',reply_markup=self._back())
            return True
        return await super()._encolar(adapter,chat_id,state,period,send)

    def _pedir_sql(self, actor, state, period):
        raise RuntimeError('PREPARAR_SOLO_LOTE')


class PrepararLoteEntregas:
    """Lo terminado de cada cliente, apenas termina: resumen y libros, revalidando el acceso."""

    def __init__(self, flow: PrepararLoteFlow) -> None:
        self.flow = flow

    async def poll(self, bot) -> None:
        rows = await asyncio.to_thread(self.flow._write,
            'SELECT row_to_json(e)::text FROM console.fn_portal_iva_preparar_entregas_telegram() e;')
        for row in rows:
            if not isinstance(row, dict):
                continue
            chat = int(row['telegram_id'])
            claimed = await asyncio.to_thread(self.flow._write,
                f"SELECT console.fn_portal_iva_preparar_entregada_telegram({int(row['id_lectura'])},{int(row['id_contribuyente'])})::text;")
            if claimed != [True] or (row.get('estado') == 'cancelada' and row.get('motivo') == CANCELADA_ANTES):
                continue
            head = f"{TITULO} · {row['nombre_legal']} · {row['periodo']}"
            if not row.get('autorizado'):
                await bot.send_message(chat_id=chat, text=f'{TITULO} · {row["periodo"]}: terminó, pero tu usuario ya no tiene acceso a ese contribuyente. No se entregó el resultado.')
                continue
            if row['estado'] != 'terminada':
                await bot.send_message(chat_id=chat, text=f"{head}\n{'Cancelada' if row['estado'] == 'cancelada' else 'No se completó'}. {row.get('motivo') or ''}".rstrip())
                continue
            archivos = [a for a in (row.get('archivos') or []) if isinstance(a, dict)]
            csv = [a for a in archivos if str(a.get('nombre', '')).lower().endswith('.csv')] or archivos
            await bot.send_message(chat_id=chat, text=f'{head}\nPeríodo preparado en ARCA. Te mando los libros; también quedan en Documentos. Revisá la clasificación de compras en Padrón ACT/Dom antes de liquidar.')
            for a in csv:
                try:
                    path = archivo_guardado(None, self.flow.clients_root, a['ruta'], a.get('sha256'))
                    if path.stat().st_size > 45_000_000:
                        raise ValueError('archivo_grande')
                    with open(path, 'rb') as stream:
                        await bot.send_document(chat_id=chat, document=stream, filename=path.name, caption='También en Documentos')
                except Exception:
                    logger.warning('[PREPARAR-LOTE] libro no entregado; conservado en Documentos')
                    await bot.send_message(chat_id=chat, text=f"{a.get('nombre') or 'Un libro'} quedó en Documentos, pero no pude mandarlo por acá. Descargalo desde el panel.")

    async def run(self, adapter) -> None:
        while True:
            try:
                if adapter._bot is not None:
                    await self.poll(adapter._bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[PREPARAR-LOTE] entrega: diagnóstico técnico no disponible')
            await asyncio.sleep(5)
