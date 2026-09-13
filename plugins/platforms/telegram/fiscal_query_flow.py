"""CCMA/SCT entry flows recovered from Lea's previous fiscal keyboard.

Credential selection stays local; SCT runs directly, CCMA passes an opaque
selection to its local runner. Neither flow dispatches an agent turn.
"""
from __future__ import annotations
import asyncio
import dataclasses
import logging
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path as _Path
from typing import Dict, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from plugins.platforms.telegram.menu_buttons import menu_label
from plugins.platforms.telegram.fiscal_execution import freeze_credentials, terminate_owned_group
from plugins.platforms.telegram.fiscal_runtime import browser_environment, require_fiscal_runtime, unavailable_message
from plugins.platforms.telegram.fiscal_credentials import canonical_access
from plugins.platforms.telegram.fiscal_interaction import communicate as interactive_communicate

logger = logging.getLogger(__name__)
_WORKFLOW_MENU_CANCEL = "✖️ Cancelar"
_WORKFLOW_MENU_OUTPUT_DIR = str(_Path.home() / "hermes-workspace" / "output")
_SCT_DISPATCH_TIMEOUT_SECONDS = 240
_SCT_RESULT_PATTERN = re.compile(r"^result=([a-z0-9_]+)$", re.MULTILINE)
_WORKFLOW_MENU_TIMEOUT_SECONDS = 600


@dataclasses.dataclass
class _WorkflowMenuState:
    """Ephemeral data for one fiscal workflow selection in one private chat."""

    skill_command: str
    label: str
    nonce: str = dataclasses.field(default_factory=lambda: secrets.token_hex(5))
    contributor_id: Optional[int] = None
    slug: Optional[str] = None
    cuit: Optional[str] = None
    candidates: tuple[int, ...] = ()
    stage: str = "client"
    credential_line: Optional[int] = None
    credential_sha256: Optional[str] = None
    holder_cuit: Optional[str] = None
    captcha_response: Optional[asyncio.Future] = None
    captcha_nonce: Optional[str] = None
    captcha_message_id: Optional[int] = None
    period: Optional[str] = None
    started_monotonic: float = dataclasses.field(default_factory=time.monotonic)


class FiscalQueryFlow:
    def __init__(self, catalog=None):
        self._workflow_menu_state = {}
        self._sct_dispatch_tasks = {}
        self._sct_dispatch_processes = {}
        self._adapter = None
        self.catalog = catalog

    def bind(self, adapter):
        self._adapter = adapter
        self.send = adapter.send
        self.send_document = adapter.send_document
        self._background_tasks = adapter._background_tasks

    def _cancel_keyboard(self, state):
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            menu_label('✕', 'Cancelar'), callback_data=f'fq:cancel:{state.nonce}')]])

    def _candidate_keyboard(self, state, rows):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(menu_label('👤', f"{row['nombre']} — {self.catalog._visible_cuit(row['cuit'])}"),
                                  callback_data=f"fq:select:{state.nonce}:{row['id']}")]
            for row in rows
        ] + list(self._cancel_keyboard(state).inline_keyboard))

    async def _send_panel(self, chat_id, text, keyboard):
        await self._adapter._bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    async def _select(self, chat_id, key, state, item_id):
        # Revalidate the canonical ARCA relation; retrieve identities, never passwords.
        rows = await asyncio.to_thread(self.catalog._by_id, item_id)
        if not rows:
            await self.send(chat_id, 'La opción ya no tiene un acceso ARCA válido. Probá otra búsqueda.')
            return False
        holders = await asyncio.to_thread(self.catalog._query, f"""
            SELECT json_build_object('usuario',btrim(holder.cuit))::text
            FROM tbl_representaciones r
            JOIN tbl_entidades e ON e.id_entidad=r.id_entidad AND e.nombre='ARCA' AND e.activo
            JOIN tbl_contribuyentes holder ON holder.id_contribuyente=r.id_contribuyente_representante AND holder.activo
            JOIN tbl_accesos a ON a.id_entidad=e.id_entidad AND a.id_contribuyente=holder.id_contribuyente AND a.activo
            WHERE r.activo AND r.id_contribuyente_representado={int(item_id)};
        """)
        if len(holders) != 1:
            await self.send(chat_id, 'No pude resolver un único acceso ARCA. Revisá la representación.')
            return False
        if self._workflow_menu_state.get(key) is not state:
            return False
        state.slug, state.cuit = rows[0]["slug"], rows[0]["cuit"]
        state.contributor_id = item_id
        state.holder_cuit = holders[0]['usuario']
        state.stage = 'period'
        return True

    async def callback(self, adapter, query, data, chat_id, thread_id, user_id):
        self.bind(adapter)
        key = (str(chat_id), str(user_id))
        action = data.partition(':')[2]
        state = self._workflow_menu_state.get(key)
        if state and state.stage not in ('running', 'captcha') and time.monotonic() - state.started_monotonic > _WORKFLOW_MENU_TIMEOUT_SECONDS:
            self._workflow_menu_state.pop(key, None)
            state = None
        if action.startswith(('cancel:', 'select:')):
            parts = action.split(':')
            if not state or parts[1] != state.nonce:
                await query.answer('Esta selección venció. Iniciá una consulta nueva.')
                return
            if parts[0] == 'cancel':
                self._workflow_menu_state.pop(key, None)
                task = self._sct_dispatch_tasks.get(key)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await query.answer()
                await query.edit_message_text('Operación cancelada.')
                return
            if len(parts) != 3 or not parts[2].isdigit() or state.stage != 'client' or int(parts[2]) not in state.candidates:
                await query.answer('Esta opción ya no está disponible.')
                return
            await query.answer()
            try:
                if await self._select(chat_id, key, state, int(parts[2])):
                    await self._request_period(chat_id, state)
            except Exception:
                await self.send(chat_id, 'No pude consultar la base. Probá nuevamente.')
            return
        choices = {
            'ccma': ('ccma_obligaciones_pagos', 'CCMA Obligaciones y pagos'),
            'sct': ('sct_estado_cumplimiento', 'SCT Estado de cumplimiento'),
        }
        if action not in choices:
            await query.answer('Esta opción ya no está disponible.')
            return
        if state or (key in self._sct_dispatch_tasks and not self._sct_dispatch_tasks[key].done()):
            await query.answer('Completá o cancelá la consulta fiscal en curso.')
            return
        command, label = choices[action]
        state = _WorkflowMenuState(skill_command=command, label=label)
        self._workflow_menu_state[key] = state
        await query.answer()
        await query.edit_message_text(
            f'{label}\nIngresá nombre, CUIT o slug del contribuyente.',
            reply_markup=self._cancel_keyboard(state))

    async def _request_period(self, chat_id, state):
        text = 'Ingresá el período: MM/AAAA, MM/AAAA-MM/AAAA o un año completo (AAAA).'
        if state.skill_command == 'sct_estado_cumplimiento':
            text += ' También podés enviar - sin filtro.'
        await self._send_panel(chat_id, text, self._cancel_keyboard(state))

    async def text(self, adapter, message):
        self.bind(adapter)
        return await self._handle_workflow_menu_text(message)

    async def close(self):
        tasks = list(self._sct_dispatch_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workflow_menu_state.clear()

    @staticmethod
    def _is_workflow_menu_private_message(message: Message) -> bool:
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        return str(getattr(chat_type, "value", chat_type)).lower() == "private"


    @staticmethod
    def _workflow_menu_state_key(message: Message) -> tuple[str, str]:
        chat = getattr(message, "chat", None)
        user = getattr(message, "from_user", None)
        return (str(getattr(chat, "id", "")), str(getattr(user, "id", "")))


    @staticmethod
    def _parse_ccma_period_range(text: str) -> Optional[tuple[str, str]]:
        """Normalize one CCMA month, a month range, or a calendar year."""
        compact = re.sub(r"\s+", "", text)
        if re.fullmatch(r"\d{4}", compact):
            return f"01/{compact}", f"12/{compact}"
        match = re.fullmatch(
            r"(0[1-9]|1[0-2])/(\d{4})(?:[-–](0[1-9]|1[0-2])/(\d{4}))?",
            compact,
        )
        if not match:
            return None
        from_period = f"{match.group(1)}/{match.group(2)}"
        to_period = f"{match.group(3) or match.group(1)}/{match.group(4) or match.group(2)}"
        if (int(match.group(4) or match.group(2)), int(match.group(3) or match.group(1))) < (
            int(match.group(2)),
            int(match.group(1)),
        ):
            return None
        return from_period, to_period


    @classmethod
    def _parse_sct_period_selection(cls, text: str) -> Optional[tuple[str, str, str]]:
        """Normalize the menu's CCMA-style period input for the SCT runner."""
        compact = re.sub(r"\s+", "", text)
        if compact == "-":
            return "empty", "", ""
        if re.fullmatch(r"\d{4}", compact):
            return "range", f"{compact}0000", f"{compact}1231"
        period_range = cls._parse_ccma_period_range(compact)
        if period_range is None:
            return None
        from_period, to_period = period_range
        from_month, from_year = from_period.split("/")
        to_month, to_year = to_period.split("/")
        if from_year != to_year:
            return None
        return "range", f"{from_year}{from_month}00", f"{to_year}{to_month}31"


    async def _resolve_sct_credential_line(self, usuario: str, represented: str) -> Optional[tuple[int, str]]:
        """Resolve an SCT credential pair to an opaque line plus a CSV integrity fingerprint."""
        hermes_home = _Path(os.environ.get("HERMES_HOME", _Path.home() / ".hermes"))
        resolver = hermes_home / "skills" / "productivity" / "sct-estado-cumplimiento" / "scripts" / "arca_pair_resolver.py"
        if not resolver.is_file():
            logger.error("[Telegram] SCT credential-pair resolver is unavailable")
            return None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(resolver),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(f"{usuario}\n{represented}\n".encode("ascii")),
                timeout=5,
            )
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            logger.warning("[Telegram] SCT credential-pair resolver timed out")
            return None
        except OSError:
            logger.warning("[Telegram] SCT credential-pair resolver could not start")
            return None
        if process.returncode != 0:
            logger.warning("[Telegram] SCT credential-pair resolver returned a non-zero status")
            return None
        match = re.fullmatch(
            r"result=selected\nline=(\d+)\ncsv_sha256=([a-f0-9]{64})\n?",
            stdout.decode("ascii", errors="ignore"),
        )
        if not match:
            return None
        line = int(match.group(1))
        return (line, match.group(2)) if line >= 2 else None


    @staticmethod
    def _sct_runner_status(stdout: bytes) -> Optional[str]:
        """Return only the runner's constrained result code, never its detail."""
        match = _SCT_RESULT_PATTERN.search(stdout.decode("utf-8", errors="ignore"))
        return match.group(1) if match else None


    def _sct_dispatch_paths(self) -> tuple[_Path, _Path, _Path, _Path, _Path]:
        """Allocate opaque output paths without incorporating chat or tax identifiers."""
        token = f"{int(time.time() * 1000)}-{secrets.token_hex(6)}"
        output_dir = _Path(_WORKFLOW_MENU_OUTPUT_DIR)
        private_dir = output_dir / "private"
        return (
            output_dir / f"sct_estado_cumplimiento_{token}.xlsx",
            private_dir / f"sct_estado_cumplimiento_{token}_fuente.csv",
            private_dir / f"sct_estado_cumplimiento_{token}_login.png",
            private_dir / f"sct_estado_cumplimiento_{token}_service.png",
            private_dir / f"sct_estado_cumplimiento_{token}_result.png",
        )


    def _sct_runner_env(
        self,
        *,
        credential_line: int,
        credential_sha256: str,
        period_mode: str,
        period_from: str,
        period_until: str,
        source_csv: _Path,
        login_screenshot: _Path,
        service_screenshot: _Path,
        result_screenshot: _Path,
        credential_file: Optional[_Path] = None,
    ) -> Dict[str, str]:
        """Build the complete, non-echoing environment passed to the SCT runner."""
        return {
            **browser_environment(_Path(os.environ.get("HERMES_HOME", _Path.home() / ".hermes"))),
            "ARCA_CSV_FILE": str(credential_file if credential_file is not None else
                os.environ.get("ARCA_CSV_FILE", _Path(os.environ.get("HERMES_HOME", _Path.home() / ".hermes")) / ".arca.csv")),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "ARCA_CSV_LINE": str(credential_line),
            "ARCA_CSV_SHA256": credential_sha256,
            "SCT_PERIOD_MODE": period_mode,
            "SCT_PERIOD_FROM": period_from,
            "SCT_PERIOD_UNTIL": period_until,
            "SCT_EXPORT_FILE": str(source_csv),
            "SCT_LOGIN_FAILURE_SCREENSHOT": str(login_screenshot),
            "SCT_SERVICE_FAILURE_SCREENSHOT": str(service_screenshot),
            "SCT_RESULT_SCREENSHOT": str(result_screenshot),
        }


    async def _start_ccma_dispatch(self, **kwargs):
        from plugins.platforms.telegram.ccma_dispatch import run_ccma
        key = kwargs['state_key']
        if key in self._sct_dispatch_tasks and not self._sct_dispatch_tasks[key].done():
            await self.send(kwargs['chat_id'], 'Ya hay una consulta en curso.')
            return
        await self.send(kwargs['chat_id'], 'Consulta CCMA iniciada. Consultando ARCA para el período solicitado…')
        task = asyncio.create_task(run_ccma(self, **kwargs))
        self._sct_dispatch_tasks[key] = task
        self._background_tasks.add(task)
        def done(finished):
            self._background_tasks.discard(finished)
            if self._sct_dispatch_tasks.get(key) is finished:
                self._sct_dispatch_tasks.pop(key, None)
                self._workflow_menu_state.pop(key, None)
            if not finished.cancelled():
                finished.exception()
        task.add_done_callback(done)

    async def _start_sct_dispatch(
        self,
        *,
        chat_id: str,
        state_key: tuple[str, str],
        credential_line: int,
        credential_sha256: str,
        period_mode: str,
        period_from: str,
        period_until: str,
        period_label: str,
        client_slug: str,
        client_cuit: str,
        contributor_id: Optional[int] = None,
        holder_cuit: Optional[str] = None,
    ) -> None:
        """Launch an SCT runner task directly, bypassing the general agent and its tools."""
        existing = self._sct_dispatch_tasks.get(state_key)
        if existing is not None and not existing.done():
            await self.send(chat_id, "Ya hay una consulta SCT en curso. Esperá su resultado o tocá Cancelar.")
            return

        task = asyncio.get_running_loop().create_task(
            self._run_sct_dispatch(
                chat_id=chat_id,
                state_key=state_key,
                credential_line=credential_line,
                credential_sha256=credential_sha256,
                period_mode=period_mode,
                period_from=period_from,
                period_until=period_until,
                period_label=period_label,
                client_slug=client_slug,
                client_cuit=client_cuit,
                contributor_id=contributor_id, holder_cuit=holder_cuit,
            )
        )
        self._sct_dispatch_tasks[state_key] = task
        self._background_tasks.add(task)

        def _clear_finished_dispatch(finished: asyncio.Task) -> None:
            self._workflow_menu_state.pop(state_key, None)
            self._background_tasks.discard(finished)
            self._sct_dispatch_processes.pop(state_key, None)
            if self._sct_dispatch_tasks.get(state_key) is finished:
                self._sct_dispatch_tasks.pop(state_key, None)
            finished.exception() if not finished.cancelled() else None

        task.add_done_callback(_clear_finished_dispatch)
        await self.send(chat_id, "Consulta SCT iniciada. El runner se ejecuta localmente en modo lectura.")


    async def _run_sct_dispatch(
        self,
        *,
        chat_id: str,
        state_key: tuple[str, str],
        credential_line: int,
        credential_sha256: str,
        period_mode: str,
        period_from: str,
        period_until: str,
        period_label: str,
        client_slug: str,
        client_cuit: str,
        contributor_id: Optional[int] = None,
        holder_cuit: Optional[str] = None,
    ) -> None:
        """Execute and deliver the bounded SCT runner without involving an LLM."""
        hermes_home = _Path(os.environ.get("HERMES_HOME", _Path.home() / ".hermes"))
        skill_dir = hermes_home / "skills" / "productivity" / "sct-estado-cumplimiento"
        probe = skill_dir / "scripts" / "sct_probe.js"
        xlsx_builder = skill_dir / "scripts" / "sct_xlsx.py"
        node = shutil.which("node")
        python = getattr(self.catalog, 'runtime_python', None)
        try:
            if not xlsx_builder.is_file():
                raise RuntimeError('fiscal_runtime_missing')
            await require_fiscal_runtime(python, node, probe, hermes_home)
        except RuntimeError as error:
            await self.send(chat_id, unavailable_message('SCT', str(error)))
            return
        from plugins.platforms.telegram.ccma_artifact import sct_destination, publish_named
        try:
            destination_dir, destination_name = sct_destination(
                _Path(os.environ.get('CONTABOT_CLIENTES_ROOT', _Path.home() / 'clientes')),
                client_slug, client_cuit, period_mode, period_from, period_until)
        except ValueError:
            await self.send(chat_id, 'SCT no pudo verificar el contribuyente o período del archivo. No se consultó ARCA.')
            return

        xlsx_file, source_csv, login_screenshot, service_screenshot, result_screenshot = self._sct_dispatch_paths()
        credential_copy = source_csv.with_suffix('.access.csv')
        source_csv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        xlsx_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = self._sct_runner_env(
            credential_line=credential_line,
            credential_sha256=credential_sha256,
            period_mode=period_mode,
            period_from=period_from,
            period_until=period_until,
            source_csv=source_csv,
            login_screenshot=login_screenshot,
            service_screenshot=service_screenshot,
            result_screenshot=result_screenshot,
            credential_file=credential_copy,
        )
        process: Optional[asyncio.subprocess.Process] = None
        credential_frozen = False
        try:
            initial = None
            if contributor_id is not None:
                initial = await canonical_access(contributor_id, client_cuit, client_slug, holder_cuit)
                for name in ('ARCA_CSV_FILE', 'ARCA_CSV_LINE', 'ARCA_CSV_SHA256'):
                    environment.pop(name, None)
                environment['FISCAL_CREDENTIAL_STDIN'] = '1'
            else:  # explicit legacy caller only; Telegram always supplies contributor_id
                freeze_credentials(_Path(os.environ.get('ARCA_CSV_FILE', hermes_home / '.arca.csv')),
                                   credential_copy, credential_sha256)
                credential_frozen = True
            captcha_dir = source_csv.parent / (source_csv.stem + '-captcha')
            captcha_dir.mkdir(mode=0o700)
            environment['FISCAL_CAPTCHA_DIR'] = str(captcha_dir)
            process = await asyncio.create_subprocess_exec(
                node,
                str(probe),
                cwd=str(skill_dir),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self._sct_dispatch_processes[state_key] = process
            stdout = await interactive_communicate(self, process, state_key, chat_id, captcha_dir, initial)
            initial = None
            status = self._sct_runner_status(stdout)
            if process.returncode != 0 or status is None:
                await self.send(chat_id, "El runner SCT terminó sin un estado verificable. No se entregó ningún resultado.")
                return
            if status != "sct_exported":
                await self.send(chat_id, f"La consulta SCT terminó sin exportación: `{status}`. Revisá la evidencia privada.")
                return

            await terminate_owned_group(process)
            process = None

            process = await asyncio.create_subprocess_exec(
                str(python),
                "-B",
                str(xlsx_builder),
                str(source_csv),
                str(xlsx_file),
                "--period-label",
                period_label,
                cwd=str(skill_dir),
                env={"HOME": str(_Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": os.environ.get("LANG", "C.UTF-8")},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self._sct_dispatch_processes[state_key] = process
            await asyncio.wait_for(process.communicate(), timeout=60)
            if process.returncode != 0 or not xlsx_file.is_file():
                await self.send(chat_id, "La fuente SCT se obtuvo, pero no se pudo generar el XLSX. No se entregó un archivo incompleto.")
                return
            xlsx_file = await asyncio.to_thread(publish_named, xlsx_file, destination_dir, destination_name)
            delivery = await self.send_document(
                chat_id=chat_id,
                file_path=str(xlsx_file),
                file_name=xlsx_file.name,
                caption="Estado de Cumplimiento SCT — consulta read-only.",
            )
            if not delivery.success:
                await self.send(chat_id, "La consulta SCT finalizó, pero Telegram no pudo entregar el XLSX. No se informó una entrega inexistente.")
                return
            await self.send(chat_id, "Consulta SCT finalizada. Se entregó el XLSX; la fuente y evidencias quedan privadas.")
        except asyncio.TimeoutError:
            await self.send(chat_id, "La consulta SCT excedió el límite de ejecución y fue detenida. No se entregó ningún resultado.")
        except asyncio.CancelledError:
            raise
        except ValueError:
            await self.send(chat_id, 'SCT no pudo verificar el acceso o el CAPTCHA. Iniciá una consulta nueva.')
        except OSError:
            await self.send(chat_id, 'SCT no pudo completar la consulta por un error local. No se entregó un archivo incompleto.')
        except Exception:
            await self.send(chat_id, 'SCT no pudo completar la consulta por un error técnico. No se informó una entrega inexistente.')
        finally:
            try:
                await terminate_owned_group(process)
            finally:
                if credential_frozen:
                    credential_copy.unlink(missing_ok=True)
                self._sct_dispatch_processes.pop(state_key, None)


    async def _handle_workflow_menu_text(self, message: Message) -> bool:
        """Consume the fiscal keyboard's text replies before normal batching.

        State is keyed by chat and user. Canonical selection resolves the
        representative locally before accepting a period.
        """
        if not self._is_workflow_menu_private_message(message):
            return False

        chat_id = str(message.chat.id)
        state_key = self._workflow_menu_state_key(message)
        text = (getattr(message, "text", "") or "").strip()
        if text == _WORKFLOW_MENU_CANCEL:
            had_state = self._workflow_menu_state.pop(state_key, None) is not None
            dispatch_task = self._sct_dispatch_tasks.get(state_key)
            if dispatch_task is not None and not dispatch_task.done():
                dispatch_task.cancel()
                had_state = True
            message_text = "Operación cancelada." if had_state else "No hay una operación en curso."
            await self.send(chat_id, message_text)
            return True

        state = self._workflow_menu_state.get(state_key)
        if state is None:
            return False
        if state.stage not in ('running', 'captcha') and time.monotonic() - state.started_monotonic > _WORKFLOW_MENU_TIMEOUT_SECONDS:
            self._workflow_menu_state.pop(state_key, None)
            await self.send(
                chat_id,
                "La operación venció por inactividad. Seleccioná un flujo para empezar de nuevo.",
            )
            return True

        if state.stage == 'captcha':
            reply = getattr(message, 'reply_to_message', None)
            if (state.captcha_message_id is None
                    or getattr(reply, 'message_id', None) != state.captcha_message_id):
                await self.send(chat_id, 'Respondé a la imagen del CAPTCHA vigente, no a una anterior.')
            elif not re.fullmatch(r'[A-Za-z0-9]{4,20}', text):
                await self.send(chat_id, 'Ingresá sólo los caracteres del CAPTCHA (sin espacios).')
            elif state.captcha_response is not None and not state.captcha_response.done():
                state.captcha_response.set_result(text)
            return True
        if state.stage == 'running':
            await self.send(chat_id, 'La consulta ya está en ejecución.')
            return True
        if state.stage == 'client':
            if not text:
                await self.send(chat_id, 'Ingresá nombre, CUIT o slug del contribuyente.')
                return True
            try:
                rows = await asyncio.to_thread(self.catalog._search, text)
                if self._workflow_menu_state.get(state_key) is not state:
                    return True
                state.candidates = tuple(int(row['id']) for row in rows)
                if not rows:
                    await self.send(chat_id, 'No hay un contribuyente ARCA activo con acceso y representación válidos que coincida. Probá con nombre, CUIT o slug.')
                elif len(rows) == 1:
                    if await self._select(chat_id, state_key, state, int(rows[0]['id'])):
                        await self._request_period(chat_id, state)
                else:
                    await self._send_panel(chat_id, 'Elegí un contribuyente:', self._candidate_keyboard(state, rows))
            except Exception:
                await self.send(chat_id, 'No pude consultar la base canónica. Probá nuevamente.')
            return True
        period = (self._parse_ccma_period_range(text) if state.skill_command == 'ccma_obligaciones_pagos'
                  else self._parse_sct_period_selection(text))
        if period is None:
            await self._request_period(chat_id, state)
            return True
        try:
            if not await self._select(chat_id, state_key, state, state.contributor_id):
                return True
        except Exception:
            await self.send(chat_id, 'No pude revalidar el acceso. Probá nuevamente.')
            return True
        if state.skill_command == 'ccma_obligaciones_pagos':
            state.stage = 'running'
            await self._start_ccma_dispatch(chat_id=chat_id, state_key=state_key,
                credential_line=state.credential_line, credential_sha256=state.credential_sha256,
                contributor_id=state.contributor_id, holder_cuit=state.holder_cuit,
                period_from=period[0], period_to=period[1], client_slug=state.slug, client_cuit=state.cuit)
        else:
            state.stage = 'running'
            await self._start_sct_dispatch(
                chat_id=chat_id, state_key=state_key,
                credential_line=state.credential_line, credential_sha256=state.credential_sha256,
                contributor_id=state.contributor_id, holder_cuit=state.holder_cuit,
                period_mode=period[0], period_from=period[1], period_until=period[2], period_label=text,
                client_slug=state.slug, client_cuit=state.cuit)
        return True
