"""Constancias ARCA por Telegram; usa el mismo lote/resultado que la consola."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
import time
import uuid

from openpyxl import Workbook
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import ContributorOffer, CONTRIBUTOR_PROMPT, MULTIPLE_CONTRIBUTORS_TEXT
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.fiscal_scope import validate_search_term
from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow


@dataclass
class State:
    nonce: str
    user_id: str
    created_at: float
    stage: str = "choice"
    study_id: int | None = None
    contributor_id: int | None = None
    candidates: dict | None = None
    job_id: int | None = None
    progress_message: object | None = None
    task: asyncio.Task | None = None


class ConstanciasFlow:
    TTL = 600

    def __init__(self) -> None:
        self.states: dict[str, State] = {}

    @staticmethod
    def _key(chat_id, thread_id, user_id) -> str:
        return f"{chat_id}:{thread_id or ''}:{user_id}"

    @staticmethod
    def _literal(value: str) -> str:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"convert_from(decode('{encoded}','base64'),'UTF8')"

    @staticmethod
    def _number(value) -> int:
        value = str(value)
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
            raise ValueError("id_invalido")
        return int(value)

    @classmethod
    def _query(cls, sql: str) -> list[dict]:
        # El único transporte DB del gateway usa el perfil restringido actual.
        return VencimientosFlow._query(sql)

    @classmethod
    def _one(cls, sql: str) -> dict:
        rows = cls._query(sql)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise RuntimeError("constancia_respuesta_ambigua")
        return rows[0]

    @classmethod
    def _search(cls, telegram_id: int, term: str) -> list[dict]:
        return cls._query(
            "SELECT json_build_object('id',id_contribuyente,'nombre',nombre_legal,"
            "'slug',slug,'cuit',cuit,'study_id',id_estudio)::text FROM console.fn_constancia_telegram_buscar("
            f"{telegram_id},{cls._literal(validate_search_term(term))});"
        )

    @classmethod
    def _studies(cls, telegram_id: int) -> list[dict]:
        return cls._query(
            "SELECT json_build_object('id',id_estudio,'nombre',nombre,'cantidad',cantidad)::text "
            f"FROM console.fn_constancia_telegram_estudios({telegram_id});"
        )

    @classmethod
    def _begin(cls, telegram_id: int, study_id: int, contributor_id: int | None) -> int:
        subject = str(contributor_id) if contributor_id else "NULL"
        row = cls._one(
            "SELECT json_build_object('id_lote',console.fn_constancia_telegram_iniciar("
            f"{telegram_id},{study_id},{subject}))::text;"
        )
        return cls._number(row["id_lote"])

    @classmethod
    def _status(cls, telegram_id: int, job_id: int) -> dict:
        return cls._one(
            "SELECT console.fn_constancia_telegram_estado("
            f"{telegram_id},{job_id})::text;"
        )

    @classmethod
    def _cancel_job(cls, telegram_id: int, job_id: int) -> bool:
        row = cls._one(
            "SELECT json_build_object('cancelada',console.fn_constancia_telegram_cancelar("
            f"{telegram_id},{job_id}))::text;"
        )
        return row.get("cancelada") is True

    @classmethod
    def _results(cls, telegram_id: int, job_id: int) -> list[dict]:
        rows: list[dict] = []
        for offset in range(0, 1001, 100):
            page = cls._query(
                "SELECT row_to_json(x)::text FROM console.fn_constancia_telegram_resultados("
                f"{telegram_id},{job_id},{offset}) x;"
            )
            if len(page) > 100:
                raise RuntimeError("constancia_resultado_excesivo")
            rows.extend(page)
            if len(page) < 100:
                return rows
        raise RuntimeError("constancia_resultado_excesivo")

    @staticmethod
    def _cancel_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"ci:cancel:{nonce}")]])

    @staticmethod
    def _choice_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(menu_label("👤", "Un cliente"), callback_data=f"ci:single:{nonce}")],
            [InlineKeyboardButton(menu_label("👥", "Toda la cartera"), callback_data=f"ci:portfolio:{nonce}")],
            [InlineKeyboardButton(menu_label("‹", "ARCA"), callback_data="om:arca_consultar")],
        ])

    @staticmethod
    def _candidate_keyboard(rows: list[dict], nonce: str) -> InlineKeyboardMarkup:
        return ContributorOffer.from_rows(rows).keyboard(callback_prefix="ci", nonce=nonce,
                                                        cancel_text=menu_label("✕", "Cancelar"))

    @staticmethod
    def _summary(status: dict, single: bool) -> str:
        done, total = status["respondidos"], status["solicitados"]
        state = status["estado"]
        if state in {"pendiente", "consultando"}:
            return f"Constancias · consultando en ARCA…\n{done}/{total} contribuyentes consultados."
        if state == "cancelada":
            return f"Consulta cancelada. {done}/{total} contribuyentes quedaron consultados."
        if state == "sin_respuesta":
            return f"ARCA no respondió o la consulta quedó interrumpida. {done}/{total} consultados; los datos anteriores se conservan."
        if single:
            return "Constancia consultada. El resultado se muestra abajo."
        return (f"Constancias consultadas: {done}/{total}.\n"
                f"Responsable inscripto: {status['ri']} · Monotributo: {status['monotributo']} · "
                f"Ambos: {status['mixto']} · Sin verificar: {status['sin_verificar']}.")

    @staticmethod
    def _book(rows: list[dict]) -> Path:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Constancias"
        sheet.append(["Contribuyente", "Alias", "Según ARCA", "ARCA lo informa desde", "Estado", "Consultado el"])
        labels = {"ri": "Responsable inscripto", "monotributo": "Monotributo",
                  "ri_monotributo": "Responsable inscripto y monotributo"}
        for row in rows:
            period = row.get("periodo_estado")
            since = f"{period[4:]}/{period[:4]}" if period and len(period) == 6 else None
            state = row["estado"]
            situation = ("Todavía no se consultó" if state == "pendiente" else
                         labels.get(row.get("condicion"), "No se pudo verificar"))
            sheet.append([row["nombre"], row["slug"], situation,
                          since, "Sin consultar" if state == "pendiente" else state, row.get("consultado_en")])
        handle = tempfile.NamedTemporaryFile(prefix="constancias-", suffix=".xlsx", delete=False)
        try:
            path = Path(handle.name)
            handle.close()
            path.chmod(0o600)
            workbook.save(path)
            return path
        except Exception:
            handle.close()
            path.unlink(missing_ok=True)
            raise

    async def _poll(self, adapter, state: State, key: str, chat_id, thread_id) -> None:
        last = ""
        failures = 0
        while True:
            try:
                status = await asyncio.to_thread(self._status, int(state.user_id), state.job_id)
                failures = 0
            except Exception:
                failures += 1
                if failures >= 3:
                    await state.progress_message.edit_text(
                        f"No pude leer el avance de la consulta #{state.job_id}. Puede seguir en proceso; revisá Constancias más tarde.")
                    self.states.pop(key, None)
                    return
                await asyncio.sleep(3)
                continue
            text = self._summary(status, state.contributor_id is not None)
            if text != last:
                await state.progress_message.edit_text(text, reply_markup=self._cancel_keyboard(state.nonce)
                                                       if status["estado"] in {"pendiente", "consultando"} else None)
                last = text
            if status["estado"] in {"terminada", "cancelada", "sin_respuesta"}:
                break
            await asyncio.sleep(3)
        if status["estado"] == "terminada":
            try:
                rows = await asyncio.to_thread(self._results, int(state.user_id), state.job_id)
                if state.contributor_id is not None:
                    row = rows[0] if len(rows) == 1 else None
                    if row:
                        condition = {"ri": "Responsable inscripto", "monotributo": "Monotributo",
                                     "ri_monotributo": "Responsable inscripto y monotributo"}.get(row["condicion"], "No se pudo verificar")
                        period = row.get("periodo_estado")
                        since = f" · ARCA lo informa desde {period[4:]}/{period[:4]}" if period and len(period) == 6 else ""
                        await state.progress_message.edit_text(f"{row['nombre']}: {condition}{since}.\nNo se obtuvo PDF oficial; este resultado son datos del padrón ARCA.")
                else:
                    output = await asyncio.to_thread(self._book, rows)
                    try:
                        metadata = {"thread_id": thread_id} if thread_id is not None else None
                        result = await adapter.send_document(chat_id=str(chat_id), file_path=str(output),
                                                             file_name="constancias-arca.xlsx", metadata=metadata,
                                                             caption="Constancias ARCA de la cartera consultada.")
                        if not getattr(result, "success", False):
                            raise RuntimeError("entrega_fallida")
                    finally:
                        output.unlink(missing_ok=True)
            except Exception:
                await state.progress_message.edit_text(
                    f"La consulta #{state.job_id} terminó, pero no pude mostrar o entregar el resultado. Revisalo en el panel.")
        self.states.pop(key, None)

    async def _start(self, adapter, state: State, key: str, target, chat_id, thread_id) -> None:
        try:
            job = await asyncio.to_thread(self._begin, int(state.user_id), state.study_id, state.contributor_id)
        except Exception:
            await target.edit_text("No pude iniciar la consulta de constancias. Revisá que no haya otra consulta en curso.")
            self.states.pop(key, None)
            return
        state.job_id = job
        state.stage = "running"
        state.progress_message = target
        await target.edit_text("Constancias · consultando en ARCA…\n0 contribuyentes consultados.",
                               reply_markup=self._cancel_keyboard(state.nonce))
        state.task = asyncio.create_task(self._poll(adapter, state, key, chat_id, thread_id))

    async def callback(self, adapter, query, data: str, chat_id, thread_id, user_id: str) -> bool:
        if not data.startswith("ci:"):
            return False
        key = self._key(chat_id, thread_id, user_id)
        parts = data.split(":")
        state = self.states.get(key)
        if data == "ci:start":
            if state and state.stage == "running":
                await query.answer("Ya hay una consulta en curso.")
                return True
            state = State(uuid.uuid4().hex[:10], user_id, time.monotonic())
            self.states[key] = state
            await query.answer()
            await query.edit_message_text("Constancias · ¿qué querés consultar?", reply_markup=self._choice_keyboard(state.nonce))
            return True
        if not state or len(parts) < 3 or parts[2] != state.nonce or (state.stage != "running" and time.monotonic() - state.created_at > self.TTL):
            await query.answer("Esta consulta venció. Iniciá una nueva.")
            return True
        if parts[1] == "cancel":
            await query.answer()
            if state.job_id:
                try:
                    await asyncio.to_thread(self._cancel_job, int(user_id), state.job_id)
                except Exception:
                    await query.edit_message_text("No pude cancelar la consulta; revisá el estado en el panel.")
                    return True
                await query.edit_message_text("Cancelación solicitada. La consulta se detendrá después de la respuesta en curso.")
            else:
                self.states.pop(key, None)
                await query.edit_message_text("Consulta cancelada.")
            return True
        if parts[1] == "single" and state.stage == "choice":
            state.stage = "search"
            await query.answer()
            await query.edit_message_text(f"Constancias · un cliente\n{CONTRIBUTOR_PROMPT}", reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if parts[1] == "portfolio" and state.stage == "choice":
            try:
                studies = await asyncio.to_thread(self._studies, int(user_id))
            except Exception:
                await query.answer("No pude leer tu cartera.")
                return True
            if not studies:
                await query.answer("No hay una cartera autorizada para consultar.")
                return True
            if len(studies) == 1:
                state.study_id = self._number(studies[0]["id"])
                await query.answer()
                await self._start(adapter, state, key, query.message, chat_id, thread_id)
                return True
            state.stage = "study"
            await query.answer()
            await query.edit_message_text("Elegí el estudio:", reply_markup=InlineKeyboardMarkup([
                *[[InlineKeyboardButton(str(item["nombre"])[:55], callback_data=f"ci:study:{state.nonce}:{item['id']}")]
                  for item in studies],
                [InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"ci:cancel:{state.nonce}")],
            ]))
            return True
        if parts[1] == "study" and state.stage == "study" and len(parts) == 4:
            state.study_id = self._number(parts[3])
            await query.answer()
            await self._start(adapter, state, key, query.message, chat_id, thread_id)
            return True
        if parts[1] == "select" and state.stage == "search" and len(parts) == 4:
            candidate = (state.candidates or {}).get(self._number(parts[3]))
            if not candidate:
                await query.answer("Esta opción ya no está disponible.")
                return True
            state.study_id = self._number(candidate["study_id"])
            state.contributor_id = self._number(candidate["id"])
            await query.answer()
            await self._start(adapter, state, key, query.message, chat_id, thread_id)
            return True
        await query.answer("Esta opción ya no está disponible.")
        return True

    async def text(self, adapter, message) -> bool:
        user = getattr(message, "from_user", None)
        key = self._key(message.chat_id, getattr(message, "message_thread_id", None), getattr(user, "id", ""))
        state = self.states.get(key)
        if not state or state.stage != "search":
            return False
        try:
            rows = await asyncio.to_thread(self._search, int(state.user_id), message.text or "")
            offer = ContributorOffer.from_rows(rows)
        except Exception:
            await message.reply_text("No pude buscar el contribuyente. Reintentá en unos minutos.")
            return True
        if offer.status == "empty":
            await message.reply_text("No encontré un contribuyente autorizado con ese nombre, CUIT o alias.", reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if offer.status == "single":
            candidate = offer.single
            state.study_id = self._number(candidate["study_id"])
            state.contributor_id = self._number(candidate["id"])
            progress = await message.reply_text("Iniciando consulta…")
            await self._start(adapter, state, key, progress, message.chat_id, getattr(message, "message_thread_id", None))
            return True
        state.candidates = offer.candidates
        await message.reply_text(MULTIPLE_CONTRIBUTORS_TEXT, reply_markup=self._candidate_keyboard(rows, state.nonce))
        return True
