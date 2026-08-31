"""Estado y consultas del flujo inline de DDJJ AGIP."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_CLIENTS_ROOT = "/home/pancho/clientes"
_DELIVERY_XLSX = re.compile(
    rf"^(?:{re.escape(_CLIENTS_ROOT)}/[a-z0-9]+(?:-[a-z0-9]+)*/(?P<cuit_annual>\d{{11}})/agip/"
    r"(?P<year_annual>\d{4})/anual/consultas/(?P=cuit_annual)-ddjj-iibb-agip-"
    r"(?P=year_annual)(?:-v\d{2})?\.xlsx|"
    rf"{re.escape(_CLIENTS_ROOT)}/[a-z0-9]+(?:-[a-z0-9]+)*/(?P<cuit_monthly>\d{{11}})/agip/"
    r"(?P<year_monthly>\d{4})/(?P<month>0[1-9]|1[0-2])/consultas/(?P=cuit_monthly)-"
    r"ddjj-iibb-agip-(?P=year_monthly)-(?P=month)(?:-v\d{2})?\.xlsx)$"
)


def is_valid_delivery_path(path: str) -> bool:
    """Accept only a v5 AGIP consultation XLSX for the represented CUIT."""
    return bool(_DELIVERY_XLSX.fullmatch(path or ""))


def visible_cuit(cuit: str) -> str:
    return f"{cuit[:2]}-******-{cuit[-1:]}" if re.fullmatch(r"\d{11}", cuit or "") else "CUIT oculto"


def normalize_period(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{4}", value):
        return value
    if re.fullmatch(r"\d{6}", value):
        value = f"{value[:4]}-{value[4:]}"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = value.split("-")
        if 1 <= int(month) <= 12:
            return value
    m = re.fullmatch(r"\d{2}/(\d{2})/(\d{4})", value)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{m.group(1)}"
    raise ValueError("Ingresá AAAA, AAAAMM, AAAA-MM o DD/MM/AAAA.")


@dataclass
class FlowState:
    user_id: str
    nonce: str
    stage: str
    contributor_id: int | None = None
    represented_id: int | None = None


class AgipDdjjFlow:
    """Inline flow; credentials remain in PostgreSQL and never leave the host."""
    def __init__(self) -> None:
        self.states: dict[str, FlowState] = {}

    @staticmethod
    def _key(chat_id: Any, thread_id: Any, user_id: Any) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _sql_scalar(value: str) -> str:
        return base64.b64encode(value.encode()).decode()

    def _query(self, sql: str) -> list[dict[str, Any]]:
        run = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "--dbname=contabot", "-At", "-c", sql],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        if run.returncode:
            raise RuntimeError("No se pudo consultar la base canónica")
        return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]

    def _search(self, term: str, contributor_id: int | None = None) -> list[dict[str, Any]]:
        encoded = self._sql_scalar(term.strip())
        base = f"convert_from(decode('{encoded}','base64'),'UTF8')"
        if contributor_id is None:
            scope = """
              FROM tbl_contribuyentes c
              JOIN tbl_accesos a ON a.id_contribuyente=c.id_contribuyente AND a.activo
              JOIN tbl_entidades e ON e.id_entidad=a.id_entidad AND e.nombre='AGIP'
            """
        else:
            scope = f"""
              FROM tbl_representaciones r
              JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='AGIP' AND r.activo
              JOIN tbl_contribuyentes c ON c.id_contribuyente=r.id_contribuyente_representado
              WHERE r.id_contribuyente_representante={int(contributor_id)} AND c.activo AND
            """
        where = f"(lower(c.nombre_legal) LIKE '%'||lower({base})||'%' OR c.slug=lower({base}) OR c.cuit=regexp_replace({base},'[^0-9]','','g'))"
        if contributor_id is None:
            where = "WHERE c.activo AND " + where
        sql = f"SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,'cuit',c.cuit,'slug',c.slug)::text {scope} {where} ORDER BY c.nombre_legal LIMIT 12;"
        return self._query(sql)

    async def _send(self, adapter, chat_id, text: str, keyboard=None, thread_id=None) -> None:
        kwargs = {"chat_id": chat_id, "text": text, "reply_markup": keyboard}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await adapter._bot.send_message(**kwargs)

    async def start(self, adapter, query, chat_id, thread_id, user_id) -> None:
        self.states[self._key(chat_id, thread_id, user_id)] = FlowState(user_id=str(user_id), nonce=uuid.uuid4().hex[:10], stage="contributor")
        await query.answer("Consulta DDJJ IIBB")
        await self._send(adapter, chat_id, "Ingresá nombre, CUIT o slug del contribuyente.", thread_id=thread_id)

    async def text(self, adapter, message) -> bool:
        chat_id, user_id = message.chat_id, message.from_user.id
        thread_id = getattr(message, "message_thread_id", None)
        key = self._key(chat_id, thread_id, user_id)
        state = self.states.get(key)
        if not state:
            return False
        if state.stage == "period":
            try:
                period = normalize_period(message.text)
            except ValueError as exc:
                await self._send(adapter, chat_id, str(exc), thread_id=thread_id)
                return True
            self.states.pop(key, None)
            await self._send(adapter, chat_id, f"Consulta AGIP iniciada para {period}. Te informaré el resultado en este chat.", thread_id=thread_id)
            asyncio.create_task(self._run_query(adapter, chat_id, thread_id, state, period))
            return True
        candidates = self._search(message.text, state.contributor_id if state.stage == "represented" else None)
        if not candidates:
            label = "representado" if state.stage == "represented" else "contribuyente"
            await self._send(adapter, chat_id, f"No hay {label} AGIP activo que coincida. Probá con nombre, CUIT o slug.", thread_id=thread_id)
            return True
        if len(candidates) == 1:
            await self._select(adapter, chat_id, thread_id, user_id, state, candidates[0])
            return True
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kind = "r" if state.stage == "represented" else "c"
        rows = [[InlineKeyboardButton(f"{x['nombre']} — {visible_cuit(x['cuit'])}", callback_data=f"ad:{kind}:{state.nonce}:{x['id']}")] for x in candidates]
        await self._send(adapter, chat_id, "Elegí una opción:", InlineKeyboardMarkup(rows), thread_id)
        return True

    def _represented(self, contributor_id: int) -> list[dict[str, Any]]:
        """All AGIP represented contributors for the selected credential holder."""
        return self._query(
            "SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,"
            "'cuit',c.cuit,'slug',c.slug)::text "
            "FROM tbl_representaciones r "
            "JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='AGIP' "
            "JOIN tbl_contribuyentes c ON c.id_contribuyente=r.id_contribuyente_representado "
            f"WHERE r.activo AND c.activo AND r.id_contribuyente_representante={int(contributor_id)} "
            "ORDER BY c.nombre_legal LIMIT 100;"
        )

    def _by_id(self, item_id: int, contributor_id: int | None) -> list[dict[str, Any]]:
        scope = ""
        if contributor_id is not None:
            scope = f"JOIN tbl_representaciones r ON r.id_contribuyente_representado=c.id_contribuyente AND r.id_contribuyente_representante={int(contributor_id)} AND r.activo JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='AGIP'"
        else:
            scope = "JOIN tbl_accesos a ON a.id_contribuyente=c.id_contribuyente AND a.activo JOIN tbl_entidades e ON e.id_entidad=a.id_entidad AND e.nombre='AGIP'"
        return self._query(f"SELECT json_build_object('id',c.id_contribuyente,'nombre',c.nombre_legal,'cuit',c.cuit,'slug',c.slug)::text FROM tbl_contribuyentes c {scope} WHERE c.activo AND c.id_contribuyente={int(item_id)};")

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id) -> bool:
        if data == "ad:start":
            await self.start(adapter, query, chat_id, thread_id, user_id)
            return True
        same = re.fullmatch(r"ad:s:([0-9a-f]{10})", data)
        if same:
            state = self.states.get(self._key(chat_id, thread_id, user_id))
            if not state or state.nonce != same.group(1) or state.stage != "represented" or state.contributor_id is None:
                await query.answer("Esta selección venció. Iniciá una consulta nueva.")
                return True
            found = self._by_id(state.contributor_id, state.contributor_id)
            if not found:
                await query.answer("El contribuyente no está habilitado como representado AGIP.")
                return True
            await query.answer("Mismo CUIT seleccionado")
            await self._select(adapter, chat_id, thread_id, user_id, state, found[0])
            return True
        m = re.fullmatch(r"ad:([cr]):([0-9a-f]{10}):(\d+)", data)
        if not m:
            return False
        state = self.states.get(self._key(chat_id, thread_id, user_id))
        if not state or state.nonce != m.group(2) or (m.group(1) == "c") != (state.stage == "contributor"):
            await query.answer("Esta selección venció. Iniciá una consulta nueva.")
            return True
        found = self._by_id(int(m.group(3)), state.contributor_id if state.stage == "represented" else None)
        if not found:
            await query.answer("La opción ya no está disponible.")
            return True
        await query.answer("Seleccionado")
        await self._select(adapter, chat_id, thread_id, user_id, state, found[0])
        return True

    async def _select(self, adapter, chat_id, thread_id, user_id, state, row) -> None:
        if state.stage == "contributor":
            state.contributor_id = int(row['id']); state.stage = "represented"
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            represented = self._represented(state.contributor_id)
            if not represented:
                raise RuntimeError("El contribuyente no tiene representados AGIP activos")
            buttons = [
                [InlineKeyboardButton(
                    f"{item['nombre']} — {visible_cuit(item['cuit'])}",
                    callback_data=f"ad:r:{state.nonce}:{item['id']}",
                )]
                for item in represented
            ]
            await self._send(
                adapter, chat_id,
                "Elegí el representado AGIP:",
                InlineKeyboardMarkup(buttons), thread_id,
            )
            return
        state.represented_id = int(row['id']); state.stage = "period"
        await self._send(adapter, chat_id, "Ingresá el período o fecha: AAAA, AAAAMM, AAAA-MM o DD/MM/AAAA.", thread_id=thread_id)

    async def _run_query(self, adapter, chat_id, thread_id, state, period: str) -> None:
        # The worker receives only opaque internal IDs and reads credentials locally.
        proc = await asyncio.create_subprocess_exec(
            "xvfb-run", "-a", "-s", "-screen 0 1440x1100x24 -nolisten tcp",
            "/home/pancho/hermes-workspace/agip-consulta-2025/.venv-selenium/bin/python",
            "/home/pancho/hermes-workspace/agip-consulta-2025/agip-ddjj-worker.py",
            str(state.contributor_id), str(state.represented_id), period,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await proc.communicate()
        try:
            result = next(
                json.loads(line)
                for line in reversed(raw.decode(errors="replace").splitlines())
                if line.strip().startswith("{")
            )
        except Exception:
            result = {"ok": False, "error": "El ejecutor AGIP no devolvió un resultado verificable."}
        if not result.get("ok"):
            await self._send(adapter, chat_id, f"Consulta AGIP no completada: {result.get('error','bloqueo no identificado')}", thread_id=thread_id)
            return
        xlsx = result.get("xlsx")
        path = os.path.realpath(str(xlsx or ""))
        if not (is_valid_delivery_path(path) and os.path.isfile(path)):
            await self._send(adapter, chat_id, "Consulta AGIP no completada: el Excel no pasó la validación de entrega.", thread_id=thread_id)
            return
        delivery = await adapter.send_document(
            chat_id=str(chat_id), file_path=path, file_name=os.path.basename(path),
            caption="DDJJ IIBB — Excel con importes.",
            metadata={"thread_id": thread_id} if thread_id is not None else None,
        )
        if not delivery.success:
            logger.error("[AGIP-DDJJ] XLSX delivery failed: %s", type(delivery.error).__name__)
            await self._send(adapter, chat_id, "Consulta AGIP no completada: Telegram no confirmó la entrega del Excel.", thread_id=thread_id)
            return
        logger.info("[AGIP-DDJJ] XLSX delivered message_id=%s", delivery.message_id)
        await self._send(adapter, chat_id, f"{result.get('message')} Excel enviado (mensaje {delivery.message_id}).", thread_id=thread_id)
