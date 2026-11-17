"""#191: elegir clientes de una lista con casillas, igual para todas las funciones por lote.

La lista la da la base (alcance del usuario); este módulo sólo guarda qué se tildó, filtra por
nombre o CUIT, pagina y arma el teclado. Tocar un nombre lo tilda o lo destilda; «Marcar todos»
marca los que se ven con el filtro actual; «Mis clientes» marca los asignados al usuario.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import menu_label

POR_PAGINA = 8
MAXIMO = 200


def plano(texto: str) -> str:
    texto = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', texto).strip().casefold()


@dataclass
class Lote:
    nonce: str
    items: list[dict[str, Any]]
    marcados: set[int] = field(default_factory=set)
    filtro: str = ''
    pagina: int = 0
    maximo: int = MAXIMO

    @classmethod
    def desde_filas(cls, nonce: str, rows: list[Any], maximo: int = MAXIMO) -> 'Lote':
        items = []
        for r in rows:
            if not isinstance(r, dict) or not str(r.get('id', '')).isdigit() or not r.get('nombre'):
                raise ValueError('lote_fila_invalida')
            items.append({'id': int(r['id']), 'nombre': str(r['nombre']).strip(), 'cuit': re.sub(r'\D', '', str(r.get('cuit') or '')),
                          'asignado': bool(r.get('asignado')), 'elegible': r.get('elegible', True) is not False,
                          'motivo': r.get('motivo')})
        return cls(nonce, items, maximo=maximo)

    @property
    def elegibles(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i['elegible']]

    @property
    def visibles(self) -> list[dict[str, Any]]:
        if not self.filtro:
            return self.elegibles
        f = plano(self.filtro); digitos = re.sub(r'\D', '', self.filtro)
        return [i for i in self.elegibles if f in plano(i['nombre']) or (len(digitos) >= 3 and digitos in i['cuit'])]

    @property
    def paginas(self) -> int:
        return max(1, -(-len(self.visibles) // POR_PAGINA))

    @property
    def elegidos(self) -> list[dict[str, Any]]:
        return [i for i in self.elegibles if i['id'] in self.marcados]

    def filtrar(self, texto: str) -> None:
        self.filtro = (texto or '').strip()[:60]
        self.pagina = 0

    def accion(self, accion: str, valor: str | None = None) -> str | None:
        """Aplica un toque; devuelve un aviso corto para la respuesta del botón, si hace falta."""
        validos = {i['id'] for i in self.elegibles}
        if accion == 't' and valor and valor.isdigit() and int(valor) in validos:
            cid = int(valor)
            if cid in self.marcados:
                self.marcados.discard(cid)
            elif len(self.marcados) >= self.maximo:
                return f'Hasta {self.maximo} clientes por corrida.'
            else:
                self.marcados.add(cid)
        elif accion == 'p' and valor and valor.isdigit():
            self.pagina = min(max(int(valor), 0), self.paginas - 1)
        elif accion == 'a':
            nuevos = [i['id'] for i in self.visibles if i['id'] not in self.marcados]
            espacio = self.maximo - len(self.marcados)
            self.marcados.update(nuevos[:espacio])
            if len(nuevos) > espacio:
                return f'Marqué {espacio}: hasta {self.maximo} clientes por corrida.'
        elif accion == 'n':
            self.marcados -= {i['id'] for i in self.visibles}
        elif accion == 'm':
            mios = [i['id'] for i in self.elegibles if i['asignado']]
            self.marcados = set(mios[:self.maximo])
        elif accion == 'f':
            self.filtrar('')
        return None

    def texto(self, titulo: str) -> str:
        lineas = [titulo, 'Tocá cada cliente para tildarlo. Para buscar, escribí parte del nombre o del CUIT.']
        if self.filtro:
            lineas.append(f'Filtro: «{self.filtro}» · {len(self.visibles)} de {len(self.elegibles)}.')
        no = len(self.items) - len(self.elegibles)
        if no:
            lineas.append(f'{no} no se pueden elegir para esta función: no aparecen en la lista.')
        lineas.append(f'Tildados: {len(self.marcados)}.')
        if not self.visibles:
            lineas.append('Ningún cliente coincide con el filtro.' if self.filtro else 'No hay clientes para elegir.')
        return '\n'.join(lineas)

    def teclado(self, prefix: str) -> InlineKeyboardMarkup:
        if not re.fullmatch(r'[a-z]{2}', prefix):
            raise ValueError('lote_prefijo_invalido')
        n = self.nonce
        inicio = self.pagina * POR_PAGINA
        rows = [[InlineKeyboardButton(('☑ ' if i['id'] in self.marcados else '☐ ') + i['nombre'][:48],
                                      callback_data=f'{prefix}:lt:{n}:{i["id"]}')]
                for i in self.visibles[inicio:inicio + POR_PAGINA]]
        if self.paginas > 1:
            nav = []
            if self.pagina > 0:
                nav.append(InlineKeyboardButton('‹ Anterior', callback_data=f'{prefix}:lp:{n}:{self.pagina - 1}'))
            nav.append(InlineKeyboardButton(f'{self.pagina + 1}/{self.paginas}', callback_data=f'{prefix}:lp:{n}:{self.pagina}'))
            if self.pagina < self.paginas - 1:
                nav.append(InlineKeyboardButton('Siguiente ›', callback_data=f'{prefix}:lp:{n}:{self.pagina + 1}'))
            rows.append(nav)
        if self.visibles:
            rows.append([InlineKeyboardButton(menu_label('☑', f'Marcar todos ({len(self.visibles)})'), callback_data=f'{prefix}:la:{n}'),
                         InlineKeyboardButton(menu_label('☐', 'Desmarcar todos'), callback_data=f'{prefix}:ln:{n}')])
        mios = sum(i['asignado'] for i in self.elegibles)
        if mios:
            rows.append([InlineKeyboardButton(menu_label('👤', f'Mis clientes ({mios})'), callback_data=f'{prefix}:lm:{n}')])
        if self.filtro:
            rows.append([InlineKeyboardButton(menu_label('✕', 'Quitar filtro'), callback_data=f'{prefix}:lf:{n}')])
        if self.marcados:
            rows.append([InlineKeyboardButton(menu_label('✅', f'Continuar con {len(self.marcados)}'), callback_data=f'{prefix}:lc:{n}')])
        rows.append([InlineKeyboardButton(menu_label('❌', 'Cancelar'), callback_data=f'{prefix}:cancel:{n}')])
        return InlineKeyboardMarkup(rows)


ACCIONES = {'lt': 't', 'lp': 'p', 'la': 'a', 'ln': 'n', 'lm': 'm', 'lf': 'f'}


def resumen_no_incluidos(filas: list[dict[str, Any]], limite: int = 10) -> str:
    """Los tildados que no corrieron, con su motivo; nunca más de `limite` renglones."""
    if not filas:
        return ''
    lineas = [f'No incluidos ({len(filas)}):'] + [f"• {f.get('nombre') or 'Contribuyente'}: {f.get('motivo') or 'no corresponde.'}" for f in filas[:limite]]
    if len(filas) > limite:
        lineas.append(f'… y {len(filas) - limite} más (el detalle queda en Operaciones).')
    return '\n'.join(lineas)
