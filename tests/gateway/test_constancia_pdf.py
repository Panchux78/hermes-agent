"""PDF opcional: CAPTCHA humano y archivo independiente del dato IVA."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugins.platforms.telegram.constancias_flow import ConstanciasFlow, State
from plugins.platforms.telegram.constancia_pdf import ConstanciaPdfSession, FORM


def test_public_form_rejects_wrong_identity_before_submission():
    session = ConstanciaPdfSession()
    try:
        session.submit("no-es-cuit", "ABC123")
        assert False, "se aceptó un CUIT inválido"
    except ValueError:
        pass


def test_document_ingress_is_local_only():
    assert ConstanciasFlow("http://127.0.0.1:8000").console_api_url.endswith(":8000")
    for endpoint in ("https://contabot.vectux.com", "http://example.org:8000", "http://127.0.0.1:8000/other"):
        try:
            ConstanciasFlow(endpoint)
            assert False, "se permitió un destino externo o ruta distinta"
        except ValueError:
            pass


def test_official_page_must_match_requested_cuit_before_printing():
    class Element:
        def __init__(self, driver, name):
            self.driver, self.name = driver, name

        def get_attribute(self, key):
            return "token" if self.name == "tokenCaptcha" and key == "value" else ""

        def send_keys(self, value):
            pass

        def is_displayed(self):
            return True

        def click(self):
            self.driver.current_url = "https://seti.afip.gob.ar/padron-puc-constancia-internet/ConstanciaAction.do"

        @property
        def text(self):
            return self.driver.body

    class Driver:
        current_url = FORM
        body = "CONSTANCIA DE OPCIÓN · CUIT: 20-12345678-9"
        def find_element(self, by, name):
            return Element(self, name)
        def find_elements(self, by, selector):
            return [Element(self, "button")]
        def print_page(self):
            return base64.b64encode(b"%PDF-1.4\n%%EOF").decode()

    session = ConstanciaPdfSession()
    session.driver = Driver()
    assert session.submit("20123456789", "ABC123").startswith(b"%PDF-")
    session.driver = Driver()
    session.driver.body = "CONSTANCIA DE INSCRIPCIÓN · CUIT: 20-00000000-1"
    try:
        session.submit("20123456789", "ABC123")
        assert False, "se imprimió una constancia de otro CUIT"
    except RuntimeError as exc:
        assert str(exc) == "constancia_identidad_no_verificada"


def test_pdf_offer_sends_current_captcha_then_archives_and_delivers():
    async def run():
        flow = ConstanciasFlow()
        progress = SimpleNamespace(edit_text=AsyncMock())
        bot = SimpleNamespace(send_photo=AsyncMock())
        adapter = SimpleNamespace(_bot=bot, send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
        state = State("nonce", "12345", 0.0, stage="pdf_running", study_id=7,
                      contributor_id=9, job_id=44, cuit="20123456789", slug="ejemplo",
                      progress_message=progress)
        key = flow._key(55, None, 12345)
        flow.states[key] = state
        with patch.object(ConstanciaPdfSession, "start", return_value=b"png"), \
             patch.object(ConstanciaPdfSession, "submit", return_value=b"%PDF-1.4\n%%EOF") as submit, \
             patch.object(ConstanciaPdfSession, "close"), \
             patch.object(flow, "_pdf_ticket", return_value="12345678-1234-1234-1234-123456789abc") as ticket, \
             patch.object(flow, "_archive_pdf", return_value={"nombre": "constancia-inscripcion-ejemplo.pdf"}) as archive:
            task = asyncio.create_task(flow._pdf(adapter, state, key, 55, None))
            for _ in range(100):
                if state.stage == "captcha":
                    break
                await asyncio.sleep(.01)
            assert state.stage == "captcha"
            assert bot.send_photo.await_count == 1
            assert bot.send_photo.call_args.kwargs["chat_id"] == 55
            message = SimpleNamespace(chat_id=55, message_thread_id=None,
                                      from_user=SimpleNamespace(id=12345), text="ABC123",
                                      reply_text=AsyncMock())
            assert await flow.text(adapter, message)
            await task
            submit.assert_called_once_with("20123456789", "ABC123")
            ticket.assert_called_once_with(12345, 44, 9)
            archive.assert_called_once()
            assert adapter.send_document.await_count == 1
            assert adapter.send_document.call_args.kwargs["file_name"] == "constancia-inscripcion-ejemplo.pdf"
            assert key not in flow.states
    asyncio.run(run())


def test_cancel_from_captcha_photo_does_not_try_to_edit_photo_as_text():
    async def run():
        flow = ConstanciasFlow()
        state = State("nonce", "12345", 0.0, stage="captcha")
        state.task = asyncio.create_task(asyncio.sleep(100))
        flow.states[flow._key(55, None, 12345)] = state
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        assert await flow.callback(None, query, "ci:cancel:nonce", 55, None, "12345")
        assert state.task.cancelled() or state.task.cancelling()
        query.edit_message_text.assert_not_awaited()
        await asyncio.gather(state.task, return_exceptions=True)
    asyncio.run(run())
