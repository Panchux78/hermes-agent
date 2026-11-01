"""Períodos de DDJJ SIFERE: el guion enumera, NO expresa un rango."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

PERIOD = r'(0[1-9]|1[0-2])/(20[1-9][0-9])'
HELP = ('Ingresá períodos aislados separados por " - ", por ejemplo '
        '01/2025 - 08/2025; o un rango inclusivo: 01/2025 hasta 04/2025. '
        'Usá MM/AAAA, sin repetir meses. Sólo meses cerrados, desde 2010, '
        'como en la consulta COMARB existente.')


def parse_periodos(text: str, modo: str, *, today: date | None = None) -> list[str]:
    if modo not in ('periodo', 'lote') or not isinstance(text, str) or len(text) > 4096:
        raise ValueError(HELP)
    today = today or datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date()
    boundary = today.replace(day=1)

    def month(value):
        matched = re.fullmatch(PERIOD, value)
        if not matched:
            raise ValueError('El período no cumple MM/AAAA. ' + HELP)
        d = date(int(matched[2]), int(matched[1]), 1)
        if d >= boundary:
            raise ValueError('Ingresá sólo meses ya cerrados. ' + HELP)
        return value

    raw = text.strip()
    if modo == 'periodo':
        return [month(raw)]
    continuous = re.fullmatch(rf'(?P<start>{PERIOD})\s+hasta\s+(?P<end>{PERIOD})', raw, re.I)
    if continuous:
        start, end = month(continuous['start']), month(continuous['end'])
        first = int(start[3:]) * 12 + int(start[:2]) - 1
        last = int(end[3:]) * 12 + int(end[:2]) - 1
        if last < first:
            raise ValueError('El fin del rango es anterior al inicio. ' + HELP)
        return [f'{index % 12 + 1:02d}/{index // 12:04d}' for index in range(first, last + 1)]
    parts = re.split(r'\s*-\s*', raw)
    if len(parts) < 2:
        raise ValueError(HELP)
    values = [month(p.strip()) for p in parts]
    if len(set(values)) != len(values):
        raise ValueError('Hay períodos repetidos. ' + HELP)
    return values
