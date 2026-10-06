"""Destinos documentales de las consultas ARCA de Telegram (Ágora #115).

Sólo describen qué es cada documento (sección, período, nombre base, tipo);
la ruta, la versión y la cuota las decide ``documentos_cliente.py`` de
ContaBot. Acá no se escribe nada en el árbol de clientes.
"""
from __future__ import annotations

import re
from datetime import date


def validate_identity(slug, cuit):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug or '') or not re.fullmatch(r'\d{11}', cuit or ''):
        raise ValueError('invalid_client_identity')


def _id_contribuyente(valor) -> int:
    if type(valor) is not int or valor <= 0:
        raise ValueError('invalid_contributor')
    return valor


def _mes(anio: int, mes: int) -> str:
    return f'{anio:04d}-{mes:02d}-01'


def _periodo(desde: tuple[int, int], hasta: tuple[int, int]) -> dict:
    """Carpeta, referencia y período según el contrato de productores."""
    if desde > hasta:
        raise ValueError('reversed_period')
    (da, dm), (ha, hm) = desde, hasta
    if desde == hasta:
        return {'anio': da, 'mes': dm, 'ref': f'{da:04d}-{dm:02d}', 'etiqueta': f'{dm:02d}/{da:04d}',
                'periodo_desde': _mes(da, dm), 'periodo_hasta': _mes(da, dm)}
    if da == ha and dm == 1 and hm == 12:
        return {'anio': da, 'mes': None, 'ref': f'{da:04d}', 'etiqueta': f'{da:04d}',
                'periodo_desde': _mes(da, 1), 'periodo_hasta': _mes(da, 12)}
    # Rango (aunque cruce años): carpeta del último mes, período completo.
    return {'anio': ha, 'mes': hm, 'ref': f'{da:04d}-{dm:02d}-a-{ha:04d}-{hm:02d}',
            'etiqueta': f'{dm:02d}/{da:04d}–{hm:02d}/{ha:04d}',
            'periodo_desde': _mes(da, dm), 'periodo_hasta': _mes(ha, hm)}


def _destino(*, slug, id_contribuyente, periodo, fecha=None, nombre, tipo, productor, ext) -> dict:
    destino = {
        'seccion': 'arca', 'anio': periodo['anio'], 'mes': periodo['mes'],
        'base': f'{slug}-{nombre}-arca-{periodo["ref"]}', 'ext': ext,
        'etiqueta': f'{tipo} · {periodo["etiqueta"]}', 'tipo': tipo, 'origen': 'ARCA',
        'productor': productor, 'id_contribuyente': _id_contribuyente(id_contribuyente),
    }
    if fecha is None:
        destino['periodo_desde'] = periodo['periodo_desde']
        destino['periodo_hasta'] = periodo['periodo_hasta']
    else:
        destino['fecha_documento'] = fecha.isoformat()
    return destino


def _mes_anio(valor: str) -> tuple[int, int]:
    if not re.fullmatch(r'(0[1-9]|1[0-2])/\d{4}', valor or ''):
        raise ValueError('invalid_period')
    mes, anio = valor.split('/')
    return int(anio), int(mes)


def ccma_destino(slug, cuit, id_contribuyente, period_from, period_to) -> dict:
    """Cuenta corriente (CCMA) de un mes, un año calendario o un rango MM/AAAA."""
    validate_identity(slug, cuit)
    periodo = _periodo(_mes_anio(period_from), _mes_anio(period_to))
    return _destino(slug=slug, id_contribuyente=id_contribuyente, periodo=periodo,
                    nombre='cuenta-corriente', tipo='Cuenta corriente', productor='ccma', ext='xlsx')


def sct_destino(slug, cuit, id_contribuyente, mode, start, end, *, hoy: date) -> dict:
    """Estado de cumplimiento (SCT); sin filtro es «a la fecha» de la consulta."""
    validate_identity(slug, cuit)
    if mode == 'empty' and start == end == '':
        # Sin filtro: no se inventa un ejercicio; carpeta del mes de la consulta.
        periodo = {'anio': hoy.year, 'mes': hoy.month, 'ref': hoy.isoformat(),
                   'etiqueta': f'a la fecha {hoy:%d/%m/%Y}'}
        return _destino(slug=slug, id_contribuyente=id_contribuyente, periodo=periodo, fecha=hoy,
                        nombre='estado-cumplimiento', tipo='Estado de cumplimiento',
                        productor='sct', ext='xlsx')
    if mode != 'range':
        raise ValueError('invalid_period')
    match_start = re.fullmatch(r'(\d{4})(00|0[1-9]|1[0-2])00', start or '')
    match_end = re.fullmatch(r'(\d{4})(0[1-9]|1[0-2])31', end or '')
    if not match_start or not match_end or match_start[1] != match_end[1]:
        raise ValueError('invalid_period')
    desde = (int(match_start[1]), int(match_start[2]) or 1)
    hasta = (int(match_end[1]), int(match_end[2]))
    return _destino(slug=slug, id_contribuyente=id_contribuyente, periodo=_periodo(desde, hasta),
                    nombre='estado-cumplimiento', tipo='Estado de cumplimiento',
                    productor='sct', ext='xlsx')


def vencimientos_destino(slug, cuit, id_contribuyente, fechas, ext) -> dict:
    """Vencimientos: año calendario si caen en un solo año; si no, rango de meses."""
    validate_identity(slug, cuit)
    if ext not in {'xlsx', 'ics'}:
        raise ValueError('invalid_extension')
    dias = sorted(date.fromisoformat(str(valor)) for valor in fechas)
    if not dias:
        raise ValueError('vencimientos_dates_missing')
    primero, ultimo = dias[0], dias[-1]
    if primero.year == ultimo.year:
        desde, hasta = (primero.year, 1), (primero.year, 12)
    else:
        desde, hasta = (primero.year, primero.month), (ultimo.year, ultimo.month)
    return _destino(slug=slug, id_contribuyente=id_contribuyente, periodo=_periodo(desde, hasta),
                    nombre='vencimientos', tipo='Vencimientos', productor='vencimientos', ext=ext)


def nombre_guardado(guardado: dict, respaldo: str) -> str:
    """Nombre con el que quedó catalogado (incluye -v02…), para la entrega por Telegram."""
    ruta = guardado.get('ruta') if isinstance(guardado, dict) else None
    nombre = ruta.rsplit('/', 1)[-1] if isinstance(ruta, str) else ''
    return nombre or respaldo
