"""Constancias ARCA por Telegram; usa el mismo lote/resultado que la consola."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import time
import uuid
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlparse

from openpyxl import Workbook
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.platforms.telegram.contributor_selector import ContributorOffer, CONTRIBUTOR_PROMPT, MULTIPLE_CONTRIBUTORS_TEXT
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.fiscal_scope import validate_search_term
from plugins.platforms.telegram.vencimientos_flow import VencimientosFlow
from plugins.platforms.telegram.constancia_pdf import ConstanciaPdfSession


@dataclass
class State:
    nonce: str
    user_id: str
    created_at: float
    stage: str = "choice"
    study_id: int | None = None
    contributor_id: int | None = None
    candidates: dict | None = None
    studies: dict | None = None
    expected_count: int | None = None
    confirmation: str | None = None
    job_id: int | None = None
    progress_message: object | None = None
    task: asyncio.Task | None = None
    cuit: str | None = None
    slug: str | None = None
    captcha_response: asyncio.Future | None = None


class ConstanciasFlow:
    TTL = 600

    def __init__(self, console_api_url: str = "http://127.0.0.1:8000") -> None:
        parsed = urlparse(console_api_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.port is None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("constancias_console_api_url_debe_ser_loopback")
        self.console_api_url = console_api_url.rstrip("/")
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
    def _begin(cls, telegram_id: int, study_id: int, contributor_id: int | None,
               confirmation: str | None = None) -> int:
        subject = str(contributor_id) if contributor_id else "NULL"
        confirmed = cls._literal(confirmation) if confirmation is not None else "NULL"
        row = cls._one(
            "SELECT json_build_object('id_lote',console.fn_constancia_telegram_iniciar_confirmado("
            f"{telegram_id},{study_id},{subject},{confirmed}))::text;"
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

    @classmethod
    def _pdf_ticket(cls, telegram_id: int, job_id: int, contributor_id: int) -> str:
        row = cls._one(
            "SELECT json_build_object('ticket',console.fn_constancia_telegram_pdf_ticket("
            f"{telegram_id},{job_id},{contributor_id}))::text;"
        )
        return str(uuid.UUID(row["ticket"]))

    def _archive_pdf(self, ticket: str, data: bytes) -> dict:
        request = Request(
            self.console_api_url + "/api/internal/constancias/pdf",
            data=data,
            headers={"X-Constancia-Ticket": ticket, "Content-Type": "application/pdf"},
            method="POST",
        )
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            if response.status != 201:
                raise RuntimeError("constancia_archivo_no_guardado")
            result = json.loads(response.read(4096))
        if (not isinstance(result, dict)
                or not re.fullmatch(r"constancia-inscripcion-[a-z0-9-]+\.pdf", result.get("nombre", ""))
                or result.get("sha256") != hashlib.sha256(data).hexdigest()):
            raise RuntimeError("constancia_archivo_respuesta_invalida")
        return result

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
    def _pdf_keyboard(nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Sí, descargar PDF", callback_data=f"ci:pdf_yes:{nonce}")],
            [InlineKeyboardButton("No, terminar", callback_data=f"ci:pdf_no:{nonce}")],
        ])

    @staticmethod
    async def _browser_call(function, *args):
        # Cancelar la task no cancela un to_thread en ejecución. Esperarlo evita
        # cerrar Firefox antes de que termine de abrirse o imprimir el PDF.
        pending = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await asyncio.shield(pending)
            raise

    async def _pdf(self, adapter, state: State, key: str, chat_id, thread_id) -> None:
        session = ConstanciaPdfSession()
        output = None
        archived = None
        try:
            image = await self._browser_call(session.start)
            state.captcha_response = asyncio.get_running_loop().create_future()
            state.stage = "captcha"
            photo = {"chat_id": chat_id, "photo": BytesIO(image),
                     "caption": "ARCA pide un CAPTCHA para la constancia PDF. Respondé con los 6 caracteres de esta imagen.",
                     "reply_markup": self._cancel_keyboard(state.nonce)}
            if thread_id is not None:
                photo["message_thread_id"] = thread_id
            await adapter._bot.send_photo(**photo)
            await state.progress_message.edit_text("Esperando el CAPTCHA de la constancia PDF…",
                                                   reply_markup=self._cancel_keyboard(state.nonce))
            solution = await asyncio.wait_for(state.captcha_response, timeout=120)
            state.stage = "pdf_running"
            await state.progress_message.edit_text("Generando la constancia PDF…",
                                                   reply_markup=self._cancel_keyboard(state.nonce))
            document = await self._browser_call(session.submit, state.cuit, solution)
            state.stage = "archiving"
            await state.progress_message.edit_text("Guardando la constancia en Documentos…")
            ticket = await asyncio.to_thread(self._pdf_ticket, int(state.user_id), state.job_id,
                                             state.contributor_id)
            archived = await asyncio.to_thread(self._archive_pdf, ticket, document)
            handle = tempfile.NamedTemporaryFile(prefix="constancia-", suffix=".pdf", delete=False)
            output = Path(handle.name)
            try:
                handle.write(document)
            finally:
                handle.close()
            output.chmod(0o600)
            metadata = {"thread_id": thread_id} if thread_id is not None else None
            sent = await adapter.send_document(chat_id=str(chat_id), file_path=str(output),
                                               file_name=archived["nombre"], metadata=metadata,
                                               caption="Constancia de inscripción ARCA.")
            if not getattr(sent, "success", False):
                await state.progress_message.edit_text("La constancia quedó en Documentos, pero Telegram no confirmó la entrega del PDF.")
            else:
                await state.progress_message.edit_text("Constancia PDF enviada y guardada en Documentos. La consulta IVA ya estaba terminada.")
        except asyncio.CancelledError:
            await state.progress_message.edit_text(
                "Solicitud de PDF cancelada. La consulta IVA se conserva."
                if archived is None else "La constancia quedó en Documentos; se canceló la entrega por Telegram.")
        except Exception:
            await state.progress_message.edit_text(
                "No pude obtener o guardar la constancia PDF. La consulta IVA se conserva; probá nuevamente más tarde."
                if archived is None else "La constancia quedó en Documentos, pero Telegram no confirmó su entrega.")
        finally:
            state.captcha_response = None
            if output is not None:
                output.unlink(missing_ok=True)
            try:
                await asyncio.to_thread(session.close)
            finally:
                self.states.pop(key, None)

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
            return f"La consulta no se completó. {done}/{total} consultados; sólo los resultados únicos ya verificados actualizaron la condición."
        if single:
            return "Constancia consultada. El resultado se muestra abajo."
        return (f"Constancias consultadas: {done}/{total}.\n"
                f"Responsable inscripto: {status['ri']} · Responsable Monotributo: {status['monotributo']} · "
                f"Revisar: {status['revisar']} · Sin verificar: {status['sin_verificar']}.")

    @staticmethod
    def _book(rows: list[dict]) -> Path:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Constancias"
        sheet.append(["Contribuyente", "Alias", "Según ARCA", "ARCA lo informa desde", "Estado", "Consultado el"])
        labels = {"ri": "Responsable inscripto", "monotributo": "Responsable Monotributo",
                  "ri_monotributo": "Responsable inscripto y monotributo"}
        for row in rows:
            period = row.get("periodo_estado")
            since = f"{period[4:]}/{period[:4]}" if period and len(period) == 6 else None
            state = row["estado"]
            situation = ("Revisar: ARCA no permite determinar una condición IVA única" if state == "revisar" else
                         "Todavía no se consultó" if state == "pendiente" else
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
                        condition = ("Revisar: ARCA no permite determinar una condición IVA única"
                                     if row["estado"] == "revisar" else
                                     {"ri": "Responsable inscripto", "monotributo": "Responsable Monotributo"}
                                     .get(row["condicion"], "No se pudo verificar"))
                        period = row.get("periodo_estado")
                        since = f" · ARCA lo informa desde {period[4:]}/{period[:4]}" if period and len(period) == 6 else ""
                        await state.progress_message.edit_text(
                            f"{row['nombre']}: {condition}{since}.\nLa condición se actualiza sólo si ARCA informa un resultado único.\n¿Querés descargar la constancia oficial en PDF?",
                            reply_markup=self._pdf_keyboard(state.nonce))
                        state.stage = "pdf_offer"
                        state.created_at = time.monotonic()
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
        if state.stage != "pdf_offer":
            self.states.pop(key, None)

    async def _start(self, adapter, state: State, key: str, target, chat_id, thread_id) -> None:
        try:
            job = await asyncio.to_thread(self._begin, int(state.user_id), state.study_id,
                                          state.contributor_id, state.confirmation)
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
            if state and state.stage in {"running", "pdf_running", "captcha", "archiving"}:
                await query.answer("Ya hay una solicitud en curso.")
                return True
            state = State(uuid.uuid4().hex[:10], user_id, time.monotonic())
            self.states[key] = state
            await query.answer()
            await query.edit_message_text("Constancias · ¿qué querés consultar?", reply_markup=self._choice_keyboard(state.nonce))
            return True
        if not state or len(parts) < 3 or parts[2] != state.nonce or (state.stage not in {"running", "pdf_running", "captcha", "archiving"} and time.monotonic() - state.created_at > self.TTL):
            await query.answer("Esta consulta venció. Iniciá una nueva.")
            return True
        if parts[1] == "cancel":
            if state.stage == "archiving":
                await query.answer("La constancia se está guardando; esperá el resultado.")
            elif state.stage in {"captcha", "pdf_running"}:
                await query.answer()
                if state.task:
                    state.task.cancel()
                # El botón puede estar en la foto del CAPTCHA: no se puede
                # editar esa foto como texto. _pdf actualiza el mensaje estable.
            elif state.stage == "pdf_offer":
                await query.answer()
                self.states.pop(key, None)
                await query.edit_message_text("Consulta IVA terminada. No se solicitó PDF.")
            elif state.job_id:
                await query.answer()
                try:
                    await asyncio.to_thread(self._cancel_job, int(user_id), state.job_id)
                except Exception:
                    await query.edit_message_text("No pude cancelar la consulta; revisá el estado en el panel.")
                    return True
                await query.edit_message_text("Cancelación solicitada. La consulta se detendrá después de la respuesta en curso.")
            else:
                await query.answer()
                self.states.pop(key, None)
                await query.edit_message_text("Consulta cancelada.")
            return True
        if parts[1] == "pdf_no" and state.stage == "pdf_offer":
            await query.answer()
            self.states.pop(key, None)
            await query.edit_message_text("Consulta IVA terminada. No se solicitó PDF.")
            return True
        if parts[1] == "pdf_yes" and state.stage == "pdf_offer":
            await query.answer()
            if not state.cuit or not state.contributor_id or not state.job_id:
                await query.edit_message_text("No puedo iniciar el PDF sin identidad verificada. La consulta IVA se conserva.")
                self.states.pop(key, None)
                return True
            state.stage = "pdf_running"
            await query.edit_message_text("Preparando el CAPTCHA de la constancia PDF…",
                                          reply_markup=self._cancel_keyboard(state.nonce))
            state.progress_message = query.message
            state.task = asyncio.create_task(self._pdf(adapter, state, key, chat_id, thread_id))
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
                state.expected_count = self._number(studies[0]["cantidad"])
                state.stage = "confirm"
                await query.answer()
                await query.edit_message_text(
                    f"Actualizar toda la cartera desde ARCA. Escribí {state.expected_count} para confirmar.",
                    reply_markup=self._cancel_keyboard(state.nonce))
                return True
            state.stage = "study"
            state.studies = {self._number(item["id"]): item for item in studies}
            await query.answer()
            await query.edit_message_text("Elegí el estudio:", reply_markup=InlineKeyboardMarkup([
                *[[InlineKeyboardButton(str(item["nombre"])[:55], callback_data=f"ci:study:{state.nonce}:{item['id']}")]
                  for item in studies],
                [InlineKeyboardButton(menu_label("✕", "Cancelar"), callback_data=f"ci:cancel:{state.nonce}")],
            ]))
            return True
        if parts[1] == "study" and state.stage == "study" and len(parts) == 4:
            selected = (state.studies or {}).get(self._number(parts[3]))
            if not selected:
                await query.answer("Ese estudio ya no está disponible.")
                return True
            state.study_id = self._number(selected["id"])
            state.expected_count = self._number(selected["cantidad"])
            state.stage = "confirm"
            await query.answer()
            await query.edit_message_text(
                f"Actualizar toda la cartera desde ARCA. Escribí {state.expected_count} para confirmar.",
                reply_markup=self._cancel_keyboard(state.nonce))
            return True
        if parts[1] == "select" and state.stage == "search" and len(parts) == 4:
            candidate = (state.candidates or {}).get(self._number(parts[3]))
            if not candidate:
                await query.answer("Esta opción ya no está disponible.")
                return True
            state.study_id = self._number(candidate["study_id"])
            state.contributor_id = self._number(candidate["id"])
            state.cuit = str(candidate["cuit"])
            state.slug = str(candidate["slug"])
            await query.answer()
            await self._start(adapter, state, key, query.message, chat_id, thread_id)
            return True
        await query.answer("Esta opción ya no está disponible.")
        return True

    async def text(self, adapter, message) -> bool:
        user = getattr(message, "from_user", None)
        key = self._key(message.chat_id, getattr(message, "message_thread_id", None), getattr(user, "id", ""))
        state = self.states.get(key)
        if not state or state.stage not in {"search", "confirm", "captcha"}:
            return False
        if state.stage == "captcha":
            answer = (message.text or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9]{6}", answer):
                await message.reply_text("El CAPTCHA tiene 6 letras o números. Respondé sólo con esos caracteres.")
            elif state.captcha_response and not state.captcha_response.done():
                state.captcha_response.set_result(answer)
            return True
        if state.stage == "confirm":
            if (message.text or "").strip() != str(state.expected_count):
                await message.reply_text(
                    f"La cantidad no coincide. Escribí {state.expected_count} para confirmar.",
                    reply_markup=self._cancel_keyboard(state.nonce))
                return True
            state.confirmation = str(state.expected_count)
            progress = await message.reply_text("Iniciando actualización de la cartera…")
            await self._start(adapter, state, key, progress, message.chat_id,
                              getattr(message, "message_thread_id", None))
            return True
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
            state.cuit = str(candidate["cuit"])
            state.slug = str(candidate["slug"])
            progress = await message.reply_text("Iniciando consulta…")
            await self._start(adapter, state, key, progress, message.chat_id, getattr(message, "message_thread_id", None))
            return True
        state.candidates = offer.candidates
        await message.reply_text(MULTIPLE_CONTRIBUTORS_TEXT, reply_markup=self._candidate_keyboard(rows, state.nonce))
        return True
