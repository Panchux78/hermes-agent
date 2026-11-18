"""Deducciones de IIBB desde SIFERE Web (Ágora #133): pedido por Telegram, cola COMARB.

El bot sólo encola en la base (console.fn_comarb_deducciones_*_telegram); la lectura la
hace el worker fiscal de la #134, el mismo del panel. El CAPTCHA llega por el puente
existente. La entrega revalida en la base que el usuario siga teniendo acceso al
contribuyente antes de mandar el Excel. Sólo lectura ante COMARB.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import ContributorOffer
from plugins.platforms.telegram.documentos_contabot import archivo_guardado
from plugins.platforms.telegram.fiscal_credentials import (
    lookup_connection,
    sanitized_psql_failure,
    verification_connection,
)
from plugins.platforms.telegram.fiscal_scope import (
    ACCOUNT_NOT_LINKED_MESSAGE,
    AccountNotLinked,
    FiscalScope,
    InvalidCuit,
)
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.lote_selector import ACCIONES, Lote, resumen_no_incluidos
from plugins.platforms.telegram.accesos_texto import como_cargar

logger = logging.getLogger(__name__)
_CLIENTS_ROOT = Path(os.environ.get("CONTABOT_CLIENTES_ROOT", Path.home() / "clientes"))
_STATE_TTL_SECONDS = 600
TITLE = "📑 Deducciones de IIBB"
REGIMENES = ("SIRCREB", "SIRCUPA", "SIRTAC", "SIRPEI", "SIRCAR · Retenciones", "SIRCAR · Percepciones")
ETIQUETA = {"SIRCAR · Retenciones": "SIRCAR, retenciones", "SIRCAR · Percepciones": "SIRCAR, percepciones"}
# #191 (pedido 7): el CAPTCHA puede ser de ARCA o de un organismo provincial (Rentas Tucumán); el texto no lo
# atribuye: la imagen que llega a Telegram ya dice de dónde viene.
PROGRESO = {"captcha_telegram": "Esperando tu respuesta al CAPTCHA que te mandé.",
            "consulta_en_curso": "CAPTCHA respondido; sigue la consulta…"}
RECHAZOS = {
    "solo_miba": "No se pudo verificar: el cliente tiene cargado sólo miBA, que todavía no está soportado. No se intentó ningún ingreso.",
    "regimen_no_verificado": "No se pudo verificar el régimen de Ingresos Brutos de ese período. Revisá la ficha fiscal; no se consultó SIFERE.",
    "sin_permiso": "Tu usuario no tiene permiso para ejecutar consultas. No se consultó COMARB.",
    "sin_acceso": "No se puede consultar ese contribuyente con tu acceso actual. No se consultó COMARB.",
    "clave_sin_verificar": ("No hay una clave cargada para las fuentes de ese período (ARCA o Clave Ciudad de AGIP), o la última vez fue rechazada. "
                            + como_cargar("ARCA o AGIP") + " miBA todavía no está soportado. No se inició ningún ingreso."),
    "ocupado": "Ya hay una consulta de COMARB en curso en tu estudio. Esperá que termine y volvé a pedirla.",
    "periodo_invalido": "El organismo no admite ese período. Revisá el mes solicitado.",
    "sin_estudio": "Tu usuario no pertenece a un estudio: pedí la cartera desde el panel.",
    "demasiados": "La cartera supera los 1000 contribuyentes: pedila desde el panel por partes.",
    "lote_invalido": "Elegí entre 1 y 200 clientes.",
    "varios_estudios": "Elegí clientes de un mismo estudio. No se consultó ningún organismo.",
}
# #191: lo que dice el worker de un cliente que no llegó a consultarse porque cancelaste.
CANCELADA_ANTES = 'No se consultó: cancelaste la corrida antes de llegar a este contribuyente.'


def importe(text: Any) -> str:
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return "—"
    sign = "-" if value < 0 else ""
    entero, _, dec = f"{abs(value):.2f}".partition(".")
    return f"{sign}$ {int(entero):,}".replace(",", ".") + "," + dec


# #191 (pedido 10): el mismo texto en Telegram, el panel y el Excel (deducciones_normalizar.SIN_CVU_DETALLE).
SIN_CVU_DETALLE = ('SIFERE no muestra cuentas virtuales (CVU) del cliente, así que no se trajeron recaudaciones de SIRCUPA. '
                   'Si el cliente no cobra con billeteras virtuales, no hay que hacer nada; si cobra con alguna, revisá SIRCUPA en SIFERE.')


def sin_cvu_pendiente(regimen: str, e: dict[str, Any]) -> bool:
    return (regimen == 'SIRCUPA' and e.get('estado') == 'no_verificado' and not e.get('filas')
            and (e.get('observacion') == 'sin_cvu_pendiente' or e.get('motivo') in (
                'No se observaron CVU en la consulta conjunta. Eso no demuestra ausencia de recaudaciones SIRCUPA.',
                'La consulta conjunta no aportó un resultado específico verificable de SIRCUPA.', SIN_CVU_DETALLE)))


def titulo(row: dict[str, Any]) -> str:
    return "📑 Retenciones y Percepciones" if (row.get('resultado') or {}).get('operacion') in ('agip_deducciones','arba_deducciones','tucuman_deducciones','santafe_deducciones','cordoba_deducciones') else TITLE


def resumen(row: dict[str, Any]) -> str:
    """Texto de cierre para un contribuyente: un renglón por régimen, nunca cero sin aclarar."""
    res = row.get("resultado") or {}
    head = f"{titulo(row)} · {row['nombre_legal']} · {row['periodo']}"
    if res.get("estado") == "no_cm":
        organismos = res.get("organismos") or []
        detail = "; ".join(f"{o['jurisdiccion']}: {o['organismo']}" for o in organismos)
        return (f"{head}\n\nNo es de Convenio Multilateral en {row['periodo']}: SIFERE no es su fuente y no se consultó COMARB."
                + (f"\nSus retenciones y percepciones salen de {detail}." if detail else ""))
    if row["estado"] != "terminada":
        return f"{head}\n\n{'Cancelada' if row['estado'] == 'cancelada' else 'No se pudo consultar'}. {row.get('motivo') or ''}".rstrip()
    estados = res.get("estados") or {}
    regimes=orden_regimenes((*(REGIMENES if res.get('operacion') in (None,'deducciones') else ()),*estados))
    # #191: el SIRCUPA sin CVU no es un faltante (igual que en el panel): dice qué hacer en su renglón.
    faltan = [r for r in regimes if estados.get(r, {}).get('estado') not in ('con_datos', 'sin_datos_verificado','no_aplica')
              and not sin_cvu_pendiente(r, estados.get(r, {}))]
    # #191 (pedido 5): los faltantes se nombran una sola vez, en su renglón con el motivo.
    lines = [head, "", "Terminada con faltantes." if faltan else "Terminada."]
    avisos = res.get("advertencias") or []
    # #191 (pedido 9): el mismo motivo para varias categorías se dice una sola vez.
    grupos: dict[tuple[str, str], int] = {}
    for regimen in regimes:
        e = estados.get(regimen) or {}
        name = ETIQUETA.get(regimen, regimen)
        if e.get("estado") == "con_datos":
            line = f"• {name}: {filas(e.get('filas', 0))} · {importe(e.get('importe'))}"
        elif e.get("estado") == "sin_datos_verificado":
            line = (f"• {name}: {regimen.split(' ·')[0]} confirmó que no hubo deducciones para {row['periodo']}." if regimen.split(' ·')[0] in ('AGIP','ARBA','Tucumán','Santa Fe','Córdoba')
                    else f"• {name}: SIFERE no registra deducciones de {regimen} para {row['periodo']}.")
        elif e.get('estado')=='no_aplica':
            continue
        elif sin_cvu_pendiente(regimen, e):
            line = f"• {name}: {SIN_CVU_DETALLE}"
        else:
            line = None
        aviso = next((a.split(": ", 1)[1] for a in avisos if a.startswith(regimen + ":")), None)
        if line is None:
            detalle = f"{e.get('motivo') or ''} {aviso or ''}".strip()
            grupo = (organismo_de(regimen), detalle)  # el mismo motivo se junta dentro de un organismo, no entre organismos
            if grupo in grupos:
                nombres, _ = lines[grupos[grupo]]
                nombres.append(name)
            else:
                grupos[grupo] = len(lines)
                lines.append(([name], detalle))
            continue
        if aviso and e.get("estado") != "con_datos":
            line += f" {aviso}"
        lines.append(line)
    lines = [l if isinstance(l, str) else f"• {_juntar(l[0])}: no se pudo consultar. {l[1]}".rstrip() for l in lines]
    if res.get("posibles_duplicados"):
        lines.append(f"{filas(res['posibles_duplicados'])} marcada{'' if res['posibles_duplicados'] == 1 else 's'} como posible duplicado; no se borró ninguna.")
    exportaciones = res.get('importaciones_sifere') or {}
    if exportaciones.get('paquete'):
        lines.append('Te mando también los archivos para SIFERE. Descomprimí el ZIP e importá cada TXT por separado, eligiendo su tipo y jurisdicción. No se incorporaron a ninguna DDJJ.')
    pendientes: dict[str, list[str]] = {}
    for estado in exportaciones.get('estados', []):
        # La fuente que no se pudo consultar ya figura arriba con su motivo: no se repite acá.
        if estado.get('estado') not in ('preparado', 'no_aplica', 'no_verificado'):
            pendientes.setdefault(estado['detalle'], []).append(f"{estado['organismo']} · {estado['tipo']}")
    for detalle, nombres in pendientes.items():
        lines.append(f"• Para SIFERE · {_juntar(nombres)}: {detalle}")
    return "\n".join(lines)


# #191 (pedido 3): el resumen va agrupado por organismo, en el orden del menú Organismos fiscales.
ORDEN_ORGANISMOS = ("AGIP", "ARBA", "Tucumán", "Santa Fe", "Córdoba", "COMARB")
ORDEN_CATEGORIAS = ("Retenciones", "Percepciones", "Retenciones bancarias", "Percepciones aduaneras")


def organismo_de(regimen: str) -> str:
    prefijo = regimen.split(" · ")[0]
    return prefijo if prefijo in ORDEN_ORGANISMOS[:-1] else "COMARB"


def orden_regimenes(regimenes) -> list[str]:
    def clave(r):
        org = organismo_de(r)
        if org == "COMARB":
            interno = REGIMENES.index(r) if r in REGIMENES else len(REGIMENES)
        else:
            cat = r.split(" · ", 1)[1] if " · " in r else ""
            interno = ORDEN_CATEGORIAS.index(cat) if cat in ORDEN_CATEGORIAS else len(ORDEN_CATEGORIAS)
        return (ORDEN_ORGANISMOS.index(org), interno)
    return sorted(dict.fromkeys(regimenes), key=clave)


def filas(n: Any) -> str:
    """#191 (pedido 4): «1 fila», nunca «1 filas»."""
    return f"{n} fila" if n == 1 else f"{n} filas"


def cierre_cancelada(st: dict[str, Any]) -> str:
    """#191 (lectura 614): un único aviso al cancelar; lo terminado ya se entregó cliente por cliente."""
    n = st["solicitados"]
    cancelados = st.get("cancelados") or 0
    cortados = min(st.get("cortados") or 0, cancelados)
    sin = cancelados - cortados
    fin = st["terminados"] - cancelados
    if n == 1:
        return ("Cancelada: se cortó la consulta en curso y no se entrega." if cortados else
                "Cancelada antes de empezar: no se consultó." if sin else "Cancelada.")
    partes = [f"{fin} de {n} {'terminó' if fin == 1 else 'terminaron'} y ya te mandé su resultado" if fin else
              f"no terminó ninguno de los {n}"]
    if cortados:
        partes.append(("se cortó la consulta en curso" if cortados == 1 else f"se cortaron {cortados} en curso") + " y no se entrega")
    if sin:
        partes.append(f"{sin} no llegaron a empezar" if sin > 1 else "1 no llegó a empezar")
    return "Cancelada: " + "; ".join(partes) + ". Quedan en Operaciones."


def _juntar(nombres: list[str]) -> str:
    return nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]


from plugins.platforms.telegram.period_input import SPECS, parse_period, propose_period, route_period_callback, sender

@dataclass
class State:
    user_id: str
    nonce: str
    stage: str  # buscar | lote | periodo | confirmar
    modo: str  # uno | lote | cartera
    contributor: dict[str, Any] | None = None
    offer: ContributorOffer | None = None
    lote: Lote | None = None
    periodo: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class DeduccionesScope(FiscalScope):
    """Mismo selector común; buscar no exige una clave de un organismo particular."""
    def _resolve(self,actor,term_sql,contributor_sql):
        return self.query('SELECT row_to_json(s)::text FROM console.fn_comarb_deducciones_buscar_telegram('
            f'{actor},{term_sql},{contributor_sql}) s;')


class ComarbDeduccionesFlow:
    callback_prefix = 'cd'
    title = TITLE
    organismo = 'COMARB'
    rechazos = RECHAZOS
    back_page = 'om:comarb_deducciones'
    operacion = 'deducciones'
    lote_funcion = None  # #191: las consultas de IIBB piden la lista de su propia operación
    espera = 'En espera de la consulta de deducciones.'
    cartera_text = ('Cartera de IIBB: se consultan los contribuyentes que ves, de Convenio Multilateral o locales mensuales de CABA en ese mes, '
                    'con acceso utilizable a SIFERE o AGIP. Recibís un Excel por contribuyente, con el estado de cada fuente. '
                    'En Convenio, también los archivos para SIFERE que se puedan preparar completos; si falta un dato, se informa.')

    def __init__(self, *, query_connection=None, write_connection=None, clients_root: Path | None = None) -> None:
        self.query_connection = query_connection or lookup_connection
        self.write_connection = write_connection or verification_connection
        self.clients_root = Path(clients_root or _CLIENTS_ROOT)
        self.states: dict[str, State] = {}
        self.watchers: dict[int, asyncio.Task] = {}

    # --- base -------------------------------------------------------------
    @staticmethod
    def _run(sql: str, connection) -> list[Any]:
        command, environment = connection()
        run = subprocess.run([*command, "-At", "-v", "VERBOSITY=sqlstate", "-c", sql], env=environment,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
        if run.returncode:
            sqlstate, _ = sanitized_psql_failure(run.stderr)
            logger.error("[COMARB-DEDUCCIONES] database query failed sqlstate=%s", sqlstate)
            raise RuntimeError("COMARB_DEDUCCIONES_DATABASE")
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def _query(self, sql: str) -> list[Any]:
        return self._run(sql, self.query_connection)

    def _write(self, sql: str) -> list[Any]:
        return self._run(sql, self.write_connection)

    def _scope(self) -> FiscalScope:
        return DeduccionesScope(self._query, "ARCA")

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _period(text: str) -> str | None:
        try:
            return parse_period(text, 'cd').visible[0]
        except ValueError:
            return None

    @classmethod
    def _back(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("‹", "Volver"), callback_data=cls.back_page),
                                     InlineKeyboardButton(menu_label("✕", "Cerrar"), callback_data="om:close")]])

    def _pedir_sql(self, actor: int, state: State, period: str) -> str:
        if state.modo == "cartera":
            return f"SELECT console.fn_comarb_deducciones_cartera_telegram({actor},'{period}')::text;"
        return (f"SELECT console.fn_comarb_deducciones_pedir_telegram({actor},"
                f"{int(state.contributor['id'])},'{period}')::text;")

    def _lote_sql(self, actor: int, state: State, period: str) -> str:
        ids = ','.join(str(int(i['id'])) for i in state.lote.elegidos)
        return (f"SELECT console.fn_comarb_deducciones_lote_telegram({actor},'{period}','{self.operacion}',"
                f"ARRAY[{ids}]::bigint[])::text;")

    def _previa_sql(self, actor: int, period: str) -> str:
        return f"SELECT console.fn_comarb_deducciones_cartera_previa_telegram({actor},'{period}','{self.operacion}')::text;"

    def _lista_sql(self, actor: int) -> str:
        return f"SELECT row_to_json(x)::text FROM console.fn_lote_contribuyentes_telegram({actor},'{self.lote_funcion or self.operacion}') x;"

    def _fuera_de_alcance(self, state, period, organismos):
        detail = "; ".join(f"{o['jurisdiccion']}: {o['organismo']} ({o['servicio']})" for o in organismos)
        return (f"{state.contributor['nombre']} no es de Convenio Multilateral en {period}: SIFERE no es su fuente y no se consultó COMARB."
                + (f"\nSus retenciones y percepciones salen de {detail}." if detail else "\nLa ficha no indica su jurisdicción de Ingresos Brutos."))

    # --- callbacks ----------------------------------------------------------
    async def callback(self, adapter, query, data: str, chat_id: Any, thread_id: Any, user_id: str) -> bool:
        key = self._key(chat_id, thread_id, user_id)
        if await route_period_callback(self.states, key, query, data, self.callback_prefix):
            return True
        if not data.startswith(self.callback_prefix+":"):
            return False
        parts = data.split(":")
        key = self._key(chat_id, thread_id, user_id)
        if parts[1] in ("uno", "cartera", "lote"):
            try:
                await asyncio.to_thread(self._scope().require_actor, user_id)
                await asyncio.to_thread(self._scope().require_permission, user_id, "operaciones.ejecutar")
            except AccountNotLinked:
                await query.answer("Cuenta no vinculada")
                await adapter._bot.send_message(chat_id=chat_id, text=ACCOUNT_NOT_LINKED_MESSAGE)
                return True
            except PermissionError:
                await query.answer("Sin permiso")
                await adapter._bot.send_message(chat_id=chat_id, text=self.rechazos["sin_permiso"])
                return True
            except Exception:
                await query.answer("No disponible")
                await adapter._bot.send_message(chat_id=chat_id, text=f"No pude verificar tu acceso en la base. No se consultó {self.organismo}.")
                return True
            nonce = secrets.token_hex(4)
            if parts[1] == "lote":
                return await self._abrir_lote(adapter, query, chat_id, key, user_id, nonce)
            if parts[1] == "uno":
                self.states[key] = State(user_id, nonce, "buscar", "uno")
                text = "Escribí nombre, CUIT o slug del contribuyente."
            else:
                self.states[key] = State(user_id, nonce, "periodo", "cartera")
                text = self.cartera_text + "\n\n" + SPECS[self.callback_prefix].prompt
            await query.answer()
            await adapter._bot.send_message(chat_id=chat_id, text=text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"{self.callback_prefix}:cancel:{nonce}")]]))
            return True
        state = self.states.get(key)
        if parts[1] in ACCIONES or parts[1] == "lc":
            return await self._lote_toque(adapter, query, chat_id, state, parts)
        if parts[1] == "cc":
            if not state or state.stage != "confirmar" or len(parts) != 3 or parts[2] != state.nonce:
                await query.answer("Esa confirmación ya no está vigente.")
                return True
            await query.answer("Consultando")
            self.states.pop(key, None)
            await self._encolar(adapter, chat_id, state, state.periodo,
                lambda text, reply_markup=None: adapter._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup))
            return True
        if parts[1] == "cancel":
            if state and len(parts) == 3 and parts[2] == state.nonce:
                self.states.pop(key, None)
            await query.answer("Cancelado")
            await adapter._bot.send_message(chat_id=chat_id, text=f"Cancelado. No se consultó {self.organismo}.", reply_markup=self._back())
            return True
        if parts[1] == "select" and len(parts) == 4:
            if not state or state.nonce != parts[2] or state.offer is None or not parts[3].isdigit():
                await query.answer("Esa selección ya no está vigente.")
                return True
            picked = state.offer.pick(int(parts[3]))
            fresh = await asyncio.to_thread(self._scope().by_id, user_id, int(parts[3])) if picked else []
            if len(fresh) != 1:
                await query.answer()
                await adapter._bot.send_message(chat_id=chat_id, text=self.rechazos["sin_acceso"], reply_markup=self._back())
                self.states.pop(key, None)
                return True
            await query.answer()
            await self._ask_period(adapter, chat_id, state, fresh[0])
            return True
        if parts[1] == "stop" and len(parts) == 3 and parts[2].isdigit():
            actor = FiscalScope._telegram_id(user_id)
            try:
                ok = await asyncio.to_thread(self._write, f"SELECT console.fn_comarb_deducciones_cancelar_telegram({actor},{int(parts[2])})::text;")
            except Exception:
                ok = [False]
            await query.answer(await self._aviso_cancelar(actor, int(parts[2])) if ok == [True] else "La consulta ya terminó, ya se canceló o no es tuya.")
            if ok == [True]:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return True
        await query.answer()
        return True

    async def _ask_period(self, adapter, chat_id: Any, state: State, item: dict[str, Any]) -> None:
        state.contributor = item
        state.stage = "periodo"
        state.offer = None
        await adapter._bot.send_message(chat_id=chat_id,
            text=f"{item['nombre']}.\n" + SPECS[self.callback_prefix].prompt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"{self.callback_prefix}:cancel:{state.nonce}")]]))

    # --- texto -------------------------------------------------------------
    async def text(self, adapter, msg) -> bool:
        user_id = str(getattr(msg.from_user, "id", ""))
        key = self._key(msg.chat_id, getattr(msg, "message_thread_id", None), user_id)
        state = self.states.get(key)
        if state is None:
            return False
        if time.monotonic() - state.created_at > _STATE_TTL_SECONDS:
            self.states.pop(key, None)
            await msg.reply_text(f"Pasaron más de 10 minutos: volvé a empezar desde el menú. No se consultó {self.organismo}.")
            return True
        if state.stage == "lote":
            state.lote.filtrar(msg.text)
            state.created_at = time.monotonic()
            await msg.reply_text(state.lote.texto(self._lote_titulo()), reply_markup=state.lote.teclado(self.callback_prefix))
            return True
        if state.stage == "confirmar":
            await msg.reply_text("Tocá «Sí, consultar» o «Cancelar» en el mensaje anterior. No se consultó ningún organismo.")
            return True
        if state.stage == "buscar":
            try:
                rows = await asyncio.to_thread(self._scope().search, user_id, msg.text)
            except InvalidCuit:
                await msg.reply_text("El CUIT no es válido. Revisalo y escribilo de nuevo.")
                return True
            except Exception:
                await msg.reply_text(f"No pude buscar en la base. No se consultó {self.organismo}.")
                return True
            offer = ContributorOffer.from_rows(rows)
            if offer.status == "empty":
                self.states.pop(key, None)
                await msg.reply_text(self.rechazos["sin_acceso"], reply_markup=self._back())
            elif offer.status == "single":
                await self._ask_period(adapter, msg.chat_id, state, offer.single)
            else:
                state.offer = offer
                await msg.reply_text("Hay más de un contribuyente con ese dato. Elegí uno:",
                    reply_markup=offer.keyboard(callback_prefix=self.callback_prefix, nonce=state.nonce,
                                                cancel_text=menu_label("❌", "Cancelar")))
            return True
        await propose_period(state, self.callback_prefix, msg.text, prefix=self.callback_prefix,
            send=msg.reply_text,
            submit=lambda selection: self._submit_period(adapter, msg, state, key, selection))
        return True

    async def _submit_period(self, adapter, msg, state, key, selection):
        period = selection.visible[0]
        if state.modo == "cartera":
            # #191: la cartera se confirma con la cantidad antes de encolar.
            actor = FiscalScope._telegram_id(state.user_id)
            try:
                rows = await asyncio.to_thread(self._write, self._previa_sql(actor, period))
                previa = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
            except Exception:
                previa = None
            if previa is None or previa.get("estado") != "previa":
                self.states.pop(key, None)
                return await self._respuesta(msg.reply_text, state, period, previa)
            state.stage = "confirmar"; state.periodo = period; state.created_at = time.monotonic()
            fuera = f"\n{previa['sin_clave']} quedan afuera por no tener acceso utilizable a sus fuentes." if previa.get("sin_clave") else ""
            await msg.reply_text(
                f"{self.title} · Cartera de IIBB · {period}\nVoy a consultar {previa['candidatos']} clientes, uno por uno.{fuera}\n"
                "¿Sigo? Si querés elegir algunos, cancelá y usá «Elegir clientes».",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(menu_label("✅", f"Sí, consultar {previa['candidatos']}"), callback_data=f"{self.callback_prefix}:cc:{state.nonce}")],
                    [InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"{self.callback_prefix}:cancel:{state.nonce}")]]))
            return True
        self.states.pop(key, None)
        return await self._encolar(adapter, msg.chat_id, state, period, msg.reply_text)

    async def _encolar(self, adapter, chat_id, state, period, send):
        actor = FiscalScope._telegram_id(state.user_id)
        sql = self._lote_sql(actor, state, period) if state.modo == "lote" else self._pedir_sql(actor, state, period)
        try:
            rows = await asyncio.to_thread(self._write, sql)
            answer = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
        except Exception:
            answer = None
        if answer is None or answer.get("estado") != "encolada":
            return await self._respuesta(send, state, period, answer)
        jid = int(answer["id_lectura"])
        who = (state.contributor["nombre"] if state.modo == "uno" else
               f"Lote de {answer.get('solicitados')} clientes" if state.modo == "lote" else f"Cartera de IIBB ({answer.get('solicitados')})")
        extra = (f"\n{answer['sin_clave']} quedan afuera por no tener acceso utilizable a sus fuentes."
                 if state.modo == "cartera" and answer.get("sin_clave") else "")
        fuera = resumen_no_incluidos(self._no_incluidos(answer.get("no_incluidos") or [])) if state.modo == "lote" else ""
        status = await send(f"{self.title} · {who} · {period}\n{self.espera}{extra}"
                            + (f"\n\n{fuera}" if fuera else ""),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"{self.callback_prefix}:stop:{jid}")]]))
        self.watchers[jid] = asyncio.create_task(self._watch(status, actor, jid, f"{self.title} · {who} · {period}", extra))
        return True

    async def _respuesta(self, send, state, period, answer):
        """Lo que no se encoló: siempre una respuesta, con el motivo."""
        if answer is None:
            await send(f"No pude registrar el pedido en la base. No se consultó {self.organismo}.", reply_markup=self._back())
            return True
        estado = answer.get("estado")
        if estado == "no_aplica":
            await send(str(answer.get("motivo") or "No aplica para el régimen del período.")+" No se consultó ningún organismo.",reply_markup=self._back())
            return True
        if estado == "no_cm":
            organismos = answer.get("organismos") or []
            await send(self._fuera_de_alcance(state,period,organismos), reply_markup=self._back())
            return True
        if estado == "sin_candidatos":
            extra = f" {answer.get('sin_clave')} no tienen acceso utilizable a las fuentes del período." if answer.get("sin_clave") else ""
            fuera = resumen_no_incluidos(self._no_incluidos(answer.get("no_incluidos") or []))
            await send(f"No hay contribuyentes consultables para {period}.{extra} No se inició ningún ingreso." + (f"\n\n{fuera}" if fuera else ""),
                       reply_markup=self._back())
            return True
        await send(self.rechazos.get(estado, f"No se pudo iniciar la consulta. No se consultó {self.organismo}."), reply_markup=self._back())
        return True

    # --- lote (#191) ----------------------------------------------------------
    def _no_incluidos(self, filas):
        return filas

    def _lote_titulo(self) -> str:
        return f"{self.title} · Elegir clientes"

    async def _abrir_lote(self, adapter, query, chat_id, key, user_id, nonce):
        actor = FiscalScope._telegram_id(user_id)
        try:
            rows = await asyncio.to_thread(self._query, self._lista_sql(actor))
            lote = Lote.desde_filas(nonce, rows)
        except Exception:
            await query.answer("No disponible")
            await adapter._bot.send_message(chat_id=chat_id, text=f"No pude leer tus clientes en la base. No se consultó {self.organismo}.")
            return True
        await query.answer()
        if not lote.elegibles:
            await adapter._bot.send_message(chat_id=chat_id, text="No tenés clientes para elegir en esta función. No se consultó ningún organismo.", reply_markup=self._back())
            return True
        state = State(user_id, nonce, "lote", "lote", lote=lote)
        self.states[key] = state
        await adapter._bot.send_message(chat_id=chat_id, text=lote.texto(self._lote_titulo()), reply_markup=lote.teclado(self.callback_prefix))
        return True

    async def _lote_toque(self, adapter, query, chat_id, state, parts):
        if not state or state.stage != "lote" or len(parts) < 3 or parts[2] != state.nonce:
            await query.answer("Esa lista ya no está vigente. Volvé a empezar desde el menú.")
            return True
        state.created_at = time.monotonic()
        if parts[1] == "lc":
            if not state.lote.elegidos:
                await query.answer("Tildá al menos un cliente.")
                return True
            await query.answer()
            state.stage = "periodo"
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            nombres = ", ".join(i["nombre"] for i in state.lote.elegidos[:5]) + (" y otros" if len(state.lote.elegidos) > 5 else "")
            await adapter._bot.send_message(chat_id=chat_id,
                text=f"{len(state.lote.elegidos)} clientes tildados: {nombres}.\n" + SPECS[self.callback_prefix].prompt,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("❌", "Cancelar"), callback_data=f"{self.callback_prefix}:cancel:{state.nonce}")]]))
            return True
        aviso = state.lote.accion(ACCIONES[parts[1]], parts[3] if len(parts) > 3 else None)
        await query.answer(aviso or "")
        try:
            await query.edit_message_text(state.lote.texto(self._lote_titulo()), reply_markup=state.lote.teclado(self.callback_prefix))
        except Exception:
            pass
        return True

    async def _aviso_cancelar(self, actor: int, jid: int) -> str:
        """#191 (prueba de Pancho, lectura 614): cancelar como en la industria (AWS Batch CancelJob/TerminateJob,
        terminación de pods de Kubernetes): lo no empezado queda cancelado en el instante; el cliente en curso
        recibe la orden de parar, tiene un plazo corto para cerrar y se corta, sin entregarse."""
        try:
            rows = await asyncio.to_thread(self._query, f"SELECT console.fn_comarb_deducciones_estado_telegram({actor},{jid})::text;")
            st = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
        except Exception:
            st = None
        actual = (st or {}).get("actual")
        if st is not None and not actual:
            return "Cancelada: no se consulta ningún contribuyente más."
        if actual:
            return f"Cancelando: corto la consulta de {actual['contribuyente']}, que no se entrega, y no sigo con los demás."[:200]
        return "Cancelando: corto la consulta en curso, que no se entrega, y no sigo con los demás."

    async def _watch(self, message, actor: int, jid: int, head: str, extra: str) -> None:
        """Avance en el mismo mensaje. El resultado lo manda la entrega, no este seguimiento."""
        last = None
        try:
            for _ in range(1200):
                await asyncio.sleep(3)
                try:
                    rows = await asyncio.to_thread(self._query, f"SELECT console.fn_comarb_deducciones_estado_telegram({actor},{jid})::text;")
                except Exception:
                    continue
                if len(rows) != 1 or not isinstance(rows[0], dict):
                    return
                st = rows[0]
                actual = st.get("actual") or {}
                if st["estado"] == "cancelada":
                    text = f"{head}\n{cierre_cancelada(st)}"
                    markup = None
                elif st["estado"] in ("terminada", "error"):
                    text = f"{head}\nConsulta terminada: {st['terminados']} de {st['solicitados']}. Te mando el resultado."
                    markup = None
                elif st.get("cancelacion_solicitada"):
                    # #191 (lectura 614): mientras el cliente en curso cierra, el estado dice que se está cancelando.
                    text = (f"{head}\nCancelando: corto la consulta de {actual['contribuyente']}, que no se entrega." if actual
                            else f"{head}\nCancelando…")
                    markup = None
                else:
                    now = PROGRESO.get(actual.get("progreso"), actual.get("progreso") or "En espera.")
                    text = (f"{head}\n{st['terminados']} de {st['solicitados']} listos." if st["solicitados"] > 1 else head) \
                        + (f"\n{actual['contribuyente']} · " if actual and st["solicitados"] > 1 else "\n") + now + extra
                    markup = message.reply_markup
                if text != last:
                    last = text
                    try:
                        await message.edit_text(text, reply_markup=markup)
                    except Exception:
                        pass
                if markup is None and st["estado"] in ("terminada", "error", "cancelada"):
                    return
        finally:
            self.watchers.pop(jid, None)


class ComarbDeduccionesEntregas:
    """Entrega lo terminado: resumen y Excel, sólo si el acceso sigue vigente al entregar."""

    def __init__(self, flow: ComarbDeduccionesFlow) -> None:
        self.flow = flow

    async def enviar_importaciones(self, bot, row):
        """Mismo entregador del Excel; autorización viva para el adjunto adicional."""
        if row['estado'] != 'terminada' or not (row.get('resultado') or {}).get('importaciones_sifere', {}).get('paquete'):
            return
        chat = int(row['telegram_id'])
        sql = ('SELECT row_to_json(f)::text FROM console.fn_comarb_deducciones_importaciones_telegram('
               f"{chat},{int(row['id_lectura'])},{int(row['id_contribuyente'])}) f;")
        try:
            files = await asyncio.to_thread(self.flow._query, sql)
            if len(files) != 1 or not isinstance(files[0], dict):
                await bot.send_message(chat_id=chat, text='No se pudo autorizar el envío de los archivos para SIFERE. Revisá tus permisos en el panel; los archivos se conservan en Documentos.')
                return
            meta = files[0]
            path = archivo_guardado(None, self.flow.clients_root, meta['ruta'], meta['sha256'])
            if path.suffix.lower() != '.zip' or path.stat().st_size > 45_000_000:
                raise ValueError('archivo_no_entregable')
            # La autorización se consulta otra vez después de leer y verificar el archivo.
            current = await asyncio.to_thread(self.flow._query, sql)
            if current != files:
                raise ValueError('autorizacion_cambio')
            with open(path, 'rb') as stream:
                await bot.send_document(chat_id=chat, document=stream, filename=path.name,
                    caption=f"Archivos para SIFERE · {row['periodo']}. Descomprimí el ZIP e importá cada TXT por separado, según su tipo y jurisdicción. También en Documentos. No se modificó ninguna DDJJ.")
        except Exception:
            logger.warning('[COMARB-DEDUCCIONES] importables no entregados; conservados en Documentos')
            await bot.send_message(chat_id=chat, text='Los archivos para SIFERE quedaron en Documentos, pero no pude enviarlos por Telegram. Descargalos desde el panel.')

    async def poll(self, bot) -> None:
        rows = await asyncio.to_thread(self.flow._write,
            "SELECT row_to_json(e)::text FROM console.fn_comarb_deducciones_entregas_telegram() e;")
        for row in rows:
            if not isinstance(row, dict):
                continue
            chat = int(row["telegram_id"])
            # #191 (lectura 614): lo cancelado (no empezado o cortado) no manda un mensaje por cliente:
            # el aviso único de la cancelación ya dice cuántos terminaron y cuántos no.
            silencioso = row.get("estado") == "cancelada"
            # Reclamar antes de enviar: una reconexión nunca repite la entrega.
            claimed = await asyncio.to_thread(self.flow._write,
                f"SELECT console.fn_comarb_deducciones_entregada_telegram({int(row['id_lectura'])},{int(row['id_contribuyente'])})::text;")
            if claimed != [True] or silencioso:
                # Los que no llegaron a empezar se informan juntos en el mensaje de avance.
                continue
            # Revalidar después del claim: el listado anterior puede haber sido
            # leído antes de retirar una asignación o cambiar el estudio.
            vigente = await asyncio.to_thread(self.flow._query,
                f"SELECT console.fn_comarb_deducciones_estado_telegram({chat},{int(row['id_lectura'])})::text;")
            if not row.get("autorizado") or len(vigente)!=1 or not isinstance(vigente[0],dict):
                await bot.send_message(chat_id=chat, text=(f"{titulo(row)} · {row['periodo']}: la consulta terminó, pero tu usuario ya no "
                                                          "tiene acceso a ese contribuyente. No se entregó el resultado."))
            else:
                await bot.send_message(chat_id=chat, text=resumen(row))
                if row["estado"] == "terminada" and row.get("ruta"):
                    try:
                        path = archivo_guardado(None, self.flow.clients_root, row["ruta"], row.get("sha256"))
                    except ValueError:
                        path = None
                    if path is None or path.suffix.lower() != ".xlsx":
                        await bot.send_message(chat_id=chat, text="El Excel quedó en Documentos, pero no se pudo verificar para mandarlo por acá. Descargalo desde el panel.")
                    else:
                        res = row.get("resultado") or {}
                        try:
                            if path.stat().st_size > 45_000_000:
                                raise ValueError('archivo_grande')
                            # El resumen cedió el control: volver a comprobar
                            # el permiso vivo antes de enviar el archivo.
                            vigente = await asyncio.to_thread(self.flow._query,
                                f"SELECT console.fn_comarb_deducciones_estado_telegram({chat},{int(row['id_lectura'])})::text;")
                            if len(vigente)!=1 or not isinstance(vigente[0],dict):
                                await bot.send_message(chat_id=chat,text='Tu acceso cambió antes de enviar el Excel. No se entregó el archivo; revisá tus permisos en el panel.')
                                continue
                            with open(path, "rb") as stream:
                                await bot.send_document(chat_id=chat, document=stream, filename=path.name,
                                    caption=f"{res.get('filas', 0)} filas · también en Documentos")
                        except Exception:
                            logger.warning('[COMARB-DEDUCCIONES] Excel no entregado; conservado en Documentos')
                            await bot.send_message(chat_id=chat,text='El Excel quedó guardado en Documentos, pero no pude enviarlo por Telegram. Descargalo desde el panel.')
                await self.enviar_importaciones(bot, row)

    async def run(self, adapter) -> None:
        while True:
            try:
                if adapter._bot is not None:
                    await self.poll(adapter._bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[COMARB-DEDUCCIONES] entrega: diagnóstico técnico no disponible")
            await asyncio.sleep(5)
