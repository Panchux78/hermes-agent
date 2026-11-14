"""Entrada única de período: sintaxis, políticas y confirmación de una sola ejecución.

No consulta organismos ni autoriza contribuyentes. El submit de cada flujo conserva
su revalidación de permisos y su ejecución original; sólo se llama tras confirmar.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
import re
import secrets
import time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from plugins.platforms.telegram.menu_buttons import menu_label

SYNTAX = 'Guion = rango; coma = meses sueltos. Escribí todo en un solo mensaje.'
MONTH = r'(0?[1-9]|1[0-2])/([0-9]{4})'
RANGE = rf'({MONTH})\s*(?:-|–|a|hasta)\s*({MONTH})'
DATE = r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}'
DATE_RANGE = rf'({DATE})\s*(?:-|–|a|hasta)\s*({DATE})'


def sender(adapter, chat_id, thread_id=None):
    async def send(text, **kwargs):
        if thread_id is not None:
            kwargs['message_thread_id'] = thread_id
        return await adapter._bot.send_message(chat_id=chat_id, text=text, **kwargs)
    return send


async def route_period_callback(states, key, query, data, prefix):
    state = states.get(key)
    return await period_callback(state, query, data, prefix=prefix,
        is_current=lambda: states.get(key) is state,
        discard=lambda: states.pop(key, None))


@dataclass(frozen=True)
class PeriodSpec:
    label: str
    forms: frozenset[str] = frozenset({'month'})
    max_months: int | None = None
    min_year: int = 1
    max_year: int = 9999
    closed: bool = False
    same_year: bool = False
    all_periods: bool = False
    warning_before: str | None = None
    no_future_dates: bool = False

    @property
    def prompt(self):
        options = {'month': 'el mes (MM/AAAA)', 'year': 'el año (AAAA)',
                   'range': 'el rango (MM/AAAA-MM/AAAA)',
                   'list': 'la lista (MM/AAAA, MM/AAAA)',
                   'date_range': 'el rango de fechas (DD/MM/AAAA-DD/MM/AAAA)'}
        forms = ' o '.join(options[f] for f in options if f in self.forms)
        extra = f' Hasta {self.max_months} meses.' if self.max_months else ''
        extra += f' Desde {self.min_year}.' if self.min_year > 1 else ''
        extra += ' Sólo meses ya cerrados.' if self.closed else ''
        extra += ' El rango debe estar dentro del mismo año.' if self.same_year else ''
        extra += ' También podés enviar - para todos los períodos disponibles.' if self.all_periods else ''
        return f'{self.label} · Ingresá {forms}.{extra}\n{SYNTAX}'


SPECS = {
    'descargar-presentados': PeriodSpec('CSV de períodos presentados'),
    'generar': PeriodSpec('Preparar período nuevo'),
    'descargar-lote': PeriodSpec('Lote de Libros IVA', frozenset({'range'})),
    'ddjj-iva': PeriodSpec('DDJJ de IVA a Excel', frozenset({'month', 'range'}),
                           max_months=36),
    'ccma_obligaciones_pagos': PeriodSpec('CCMA Obligaciones y pagos', frozenset({'month', 'range', 'year'})),
    'sct_estado_cumplimiento': PeriodSpec('SCT Estado de cumplimiento', frozenset({'month', 'range', 'year'}),
                                        same_year=True, all_periods=True),
    'agip-ddjj': PeriodSpec('DDJJ de IIBB', frozenset({'month', 'year'})),
    'mis-retenciones': PeriodSpec('Mis Retenciones', frozenset({'month', 'date_range'}), no_future_dates=True),
    'comarb-periodo': PeriodSpec('DDJJ presentadas', min_year=2010, max_year=2099, closed=True),
    'comarb-lote': PeriodSpec('Lote de DDJJ presentadas', frozenset({'range', 'list'}),
                             min_year=2010, max_year=2099, closed=True),
    **{prefix: PeriodSpec(label) for prefix, label in {
        'cd': 'Deducciones de IIBB', 'ar': 'AGIP · Retenciones y Percepciones',
        'ab': 'ARBA · Retenciones y Percepciones', 'tu': 'Tucumán · Retenciones y Percepciones',
        'sf': 'Santa Fe · Retenciones y Percepciones', 'co': 'Córdoba · Retenciones y Percepciones',
    }.items()},
}


@dataclass(frozen=True)
class PeriodSelection:
    months: tuple[str, ...]  # YYYY-MM, en el orden solicitado
    form: str
    year: str | None = None
    date_from: date | None = None
    date_to: date | None = None

    @property
    def visible(self):
        return tuple(f'{m[5:]}/{m[:4]}' for m in self.months)

    @property
    def summary(self):
        if self.form == 'all':
            return 'todos los períodos disponibles'
        if self.form == 'year':
            return f'{self.year}, año completo'
        if self.form == 'date_range':
            first, last = self.dates()
            return f'{first} a {last}, {(self.date_to - self.date_from).days + 1} días'
        count = len(self.months)
        label = (f'{self.visible[0]} a {self.visible[-1]}' if self.form == 'range'
                 else ', '.join(self.visible))
        return f'{label}, {count} {"mes" if count == 1 else "meses"}'

    def sct(self):
        if self.form == 'all':
            return 'empty', '', ''
        if self.form == 'year':
            return 'range', self.year + '0000', self.year + '1231'
        first, last = self.months[0], self.months[-1]
        end = calendar.monthrange(int(last[:4]), int(last[5:]))[1]
        return 'range', first.replace('-', '') + '00', last.replace('-', '') + f'{end:02d}'

    def dates(self):
        if self.form == 'date_range':
            return self.date_from.strftime('%d/%m/%Y'), self.date_to.strftime('%d/%m/%Y')
        first, last = self.months[0], self.months[-1]
        start = date(int(first[:4]), int(first[5:]), 1)
        end = date(int(last[:4]), int(last[5:]), calendar.monthrange(int(last[:4]), int(last[5:]))[1])
        return start.strftime('%d/%m/%Y'), end.strftime('%d/%m/%Y')


def _month(raw):
    match = re.fullmatch(MONTH, raw.strip(), re.ASCII)
    if not match or not 1 <= int(match[2]) <= 9999:
        raise ValueError('El mes no es válido. Usá MM/AAAA; por ejemplo, 09/2026.')
    return int(match[2]) * 12 + int(match[1]) - 1


def _segment(raw):
    match = re.fullmatch(RANGE, raw.strip(), re.I | re.ASCII)
    if not match:
        return [_month(raw)], False
    first, last = _month(match[1]), _month(match[4])
    if last < first:
        raise ValueError('El fin del rango es anterior al inicio. Revisá ambos meses.')
    return list(range(first, last + 1)), True


def _parse_indexes(raw, spec):
    if re.fullmatch(r'[0-9]{4}', raw):
        if 'year' not in spec.forms:
            raise ValueError('Esta consulta no admite un año completo. ' + spec.prompt)
        year = int(raw)
        if not 1 <= year <= 9999:
            raise ValueError('El año no es válido.')
        indexes = list(range(year * 12, year * 12 + 12)); form = 'year'
    else:
        parts = raw.split(',')
        if len(parts) > 1 and 'list' not in spec.forms:
            raise ValueError('Esta consulta no admite una lista de meses. ' + spec.prompt)
        indexes = []; is_range = False
        for part in parts:
            items, segment_range = _segment(part)
            indexes.extend(items); is_range |= segment_range
        form = 'list' if len(parts) > 1 else 'range' if is_range else 'month'
        if form not in spec.forms:
            reason = ('Esta consulta requiere el rango completo en un solo mensaje.' if spec.forms == {'range'}
                      else 'Esta consulta es de un solo mes.' if spec.forms == {'month'}
                      else 'Esta consulta no admite esa forma de período.')
            raise ValueError(reason + ' ' + spec.prompt)
    return indexes, form


def _validate_months(indexes, form, spec, today):
    if len(set(indexes)) != len(indexes):
        raise ValueError('Hay meses repetidos. Escribí cada mes una sola vez.')
    if spec.max_months and len(indexes) > spec.max_months:
        raise ValueError(f'El máximo es {spec.max_months} meses. Acortá el rango.')
    months = tuple(f'{n // 12:04d}-{n % 12 + 1:02d}' for n in indexes)
    if any(not spec.min_year <= int(m[:4]) <= spec.max_year for m in months):
        raise ValueError(f'Esta consulta admite períodos desde {spec.min_year} hasta {spec.max_year}. Revisá el período.')
    today = today or datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date()
    if spec.closed and any(date(int(m[:4]), int(m[5:]), 1) >= today.replace(day=1) for m in months):
        raise ValueError('Ingresá sólo meses ya cerrados.')
    if spec.same_year and len({m[:4] for m in months}) > 1:
        raise ValueError('En SCT el rango tiene que estar dentro de un mismo año.')
    return months


def _date_range(match, spec, today):
    if 'date_range' not in spec.forms:
        raise ValueError('Esta consulta no admite un rango de fechas. ' + spec.prompt)
    try:
        first, last = (datetime.strptime(v, '%d/%m/%Y').date() for v in match.groups())
    except ValueError:
        raise ValueError('Alguna de las fechas no existe en el calendario. ' + spec.prompt) from None
    if last < first:
        raise ValueError('La fecha final es anterior a la inicial. Revisá ambas fechas.')
    if spec.no_future_dates and last > today:
        raise ValueError('La fecha final no puede ser posterior a hoy.')
    months = tuple(f'{n//12:04d}-{n%12+1:02d}' for n in range(first.year*12+first.month-1, last.year*12+last.month))
    return PeriodSelection(months, 'date_range', date_from=first, date_to=last)


def parse_period(text, operation, *, today=None):
    spec = SPECS[operation] if isinstance(operation, str) else operation
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError('Ingresá un período de hasta 4096 caracteres. ' + spec.prompt)
    raw = text.strip()
    today = today or datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date()
    if raw == '-' and spec.all_periods:
        return PeriodSelection((), 'all')
    dates = re.fullmatch(DATE_RANGE, raw, re.I | re.ASCII)
    if dates:
        return _date_range(dates, spec, today)
    indexes, form = _parse_indexes(raw, spec)
    months = _validate_months(indexes, form, spec, today)
    return PeriodSelection(months, form, raw if form == 'year' else None)


@dataclass
class PeriodCandidate:
    nonce: str
    selection: PeriodSelection
    operation: str
    submit: Callable[[PeriodSelection], Awaitable]
    send: Callable
    created_at: float
    consumed: bool = False


def _keyboard(prefix, nonce):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(menu_label(icon, label), callback_data=f'{prefix}:period:{action}:{nonce}')]
        for icon, label, action in [('✅', 'Sí, consultar', 'yes'), ('✏️', 'Corregir', 'edit'), ('❌', 'Cancelar', 'cancel')]
    ])


async def propose_period(state, operation, text, *, prefix, send, submit):
    """Guarda un candidato antes del I/O; otro texto nunca lo reemplaza."""
    pending = getattr(state, 'period_candidate', None)
    if pending is not None:
        if pending.consumed:
            await send('Ya confirmaste este pedido. No se inició otra consulta.')
        else:
            await send(f'Tengo pendiente {pending.selection.summary}. '
                       'Si querías otro período o un rango, tocá Corregir y escribilo en un solo mensaje: '
                       '05/2025-06/2026. No se consultó ningún organismo.',
                       reply_markup=_keyboard(prefix, pending.nonce))
        return
    try:
        selection = parse_period(text, operation)
    except ValueError as exc:
        await send(str(exc))
        return
    candidate = PeriodCandidate(secrets.token_hex(8), selection, operation, submit, send, time.monotonic())
    state.period_candidate = candidate
    details = ''
    if operation == 'comarb-lote':
        # El usuario ve exactamente todos los meses; nunca se omiten con «…».
        details = '\nMeses: ' + ', '.join(selection.visible)
    spec = SPECS[operation]
    if spec.warning_before and any(m < spec.warning_before for m in selection.months):
        details += ('\nLos meses anteriores a 06/2025 pueden corresponder a otro formulario; '
                    'si no es compatible con esta consulta, se informa el motivo sin inventar importes.')
    message = f'Entendí: {selection.summary}.{details}\n¿Consulto?'
    # Telegram admite 4096 caracteres: listas máximas conservan todos los meses.
    for offset in range(0, len(message), 3800):
        await send(message[offset:offset + 3800],
                   reply_markup=_keyboard(prefix, candidate.nonce) if offset + 3800 >= len(message) else None)


def _current_candidate(candidate, parts, started, is_current):
    return (len(parts) == 4 and candidate is not None and candidate.nonce == parts[3]
            and not candidate.consumed and is_current() and time.monotonic() - started <= 600
            and time.monotonic() - candidate.created_at <= 600)


async def period_callback(state, query, data, *, prefix, is_current, discard):
    if not data.startswith(prefix + ':period:'):
        return False
    parts = data.split(':')
    candidate = getattr(state, 'period_candidate', None)
    started = getattr(state, 'created_at', getattr(state, 'started_monotonic', 0))
    if (candidate is None and len(parts) == 4 and parts[2] == 'cancel'
            and parts[3] == getattr(state, 'nonce', None)
            and is_current() and time.monotonic() - started <= 600):
        discard()
        await query.answer('Cancelado. No se inició la consulta.')
        return True
    if not _current_candidate(candidate, parts, started, is_current):
        await query.answer('Esa confirmación venció o ya fue utilizada. Volvé al menú.')
        return True
    action = parts[2]
    if action not in ('yes', 'edit', 'cancel'):
        await query.answer('Esa opción no está disponible.')
        return True
    candidate.consumed = True  # Antes de todo await: dos callbacks no duplican.
    try:
        await query.answer()
    except TelegramError:
        if is_current():
            discard()
            await candidate.send('No pude confirmar tu respuesta en Telegram. No se inició la consulta. '
                                 'Volvé a empezar desde el menú.')
        return True
    if not is_current():
        return True
    if action == 'cancel':
        discard()
        await candidate.send('Cancelado. No se inició la consulta.')
    elif action == 'edit':
        state.period_candidate = None
        cancel = InlineKeyboardMarkup([[InlineKeyboardButton(menu_label('❌', 'Cancelar'),
            callback_data=f'{prefix}:period:cancel:{state.nonce}')]])
        await candidate.send(SPECS[candidate.operation].prompt, reply_markup=cancel)
    else:
        await candidate.submit(candidate.selection)
    return True
