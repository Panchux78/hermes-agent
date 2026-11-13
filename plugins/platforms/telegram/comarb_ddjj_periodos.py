"""Compatibilidad para consumidores: la sintaxis sólo vive en period_input."""
from plugins.platforms.telegram.period_input import SPECS, parse_period

HELP = SPECS['comarb-lote'].prompt


def parse_periodos(text, modo, *, today=None):
    if modo not in ('periodo', 'lote'):
        raise ValueError(HELP)
    return list(parse_period(text, 'comarb-' + modo, today=today).visible)
