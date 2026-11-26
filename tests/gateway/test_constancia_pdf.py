"""PDF opcional: CAPTCHA automático primero (#216), humano como último recurso, y archivo independiente del dato IVA."""

import asyncio
import base64
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.telegram.constancias_flow import ConstanciasFlow, State
from plugins.platforms.telegram.constancia_pdf import CaptchaRechazado, ConstanciaPdfSession, FORM, LecturaAutomatica
from plugins.platforms.telegram.constancia_pdf import _arca_browser, archivar_pdf


@pytest.mark.parametrize('origin', ['https://127.0.0.1:8000','http://example.test:8000',
    'http://127.0.0.1:8000/ajeno','http://otro@127.0.0.1:8000','http://127.0.0.1:8000/?otro=1'])
def test_archivo_oficial_solo_se_envia_al_ingreso_local(origin):
    with patch('constancia_pdf_motor.build_opener') as opener:
        with pytest.raises(ValueError,match='loopback'):
            archivar_pdf(origin,'ticket-sintetico',b'%PDF-')
    opener.assert_not_called()


def test_public_pdf_uses_shared_arca_browser_and_closes_profile():
    driver = MagicMock()
    driver.find_element.return_value.get_attribute.side_effect = lambda key: (
        "token" if key == "value" else "data:image/png;base64,AA==")
    driver.find_element.return_value.screenshot_as_png = b"current-captcha"
    browser = MagicMock()
    browser.__enter__.return_value = driver
    with patch("plugins.platforms.telegram.constancia_pdf._arca_browser", create=True,
               return_value=browser) as shared, \
         patch("selenium.webdriver.Firefox", return_value=driver) as legacy:
        session = ConstanciaPdfSession()
        assert session.start() == b"current-captcha"
        shared.assert_called_once_with()
        legacy.assert_not_called()
        driver.set_page_load_timeout.assert_called_once_with(30)
        driver.get.assert_called_once_with(FORM)
        session.close()
        session.close()
        browser.__exit__.assert_called_once_with(None, None, None)
        assert session.driver is None


def test_public_pdf_failed_start_closes_shared_profile():
    driver = MagicMock()
    driver.get.side_effect = RuntimeError("synthetic loading failure")
    browser = MagicMock()
    browser.__enter__.return_value = driver
    with patch("plugins.platforms.telegram.constancia_pdf._arca_browser", create=True,
               return_value=browser), \
         patch("selenium.webdriver.Firefox", return_value=driver):
        session = ConstanciaPdfSession()
        with pytest.raises(RuntimeError, match="synthetic loading failure"):
            session.start()
        browser.__exit__.assert_called_once_with(None, None, None)
        assert session.driver is None


def test_public_pdf_provider_is_loaded_from_configured_installed_shared(tmp_path, monkeypatch):
    root = tmp_path / "skills/shared/arca"
    root.mkdir(parents=True)
    factory = MagicMock()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch.dict(sys.modules, {"arca_browser": SimpleNamespace(firefox=factory)}), \
         patch.object(sys, "path", list(sys.path)):
        assert _arca_browser() is factory.return_value
        assert sys.path[0] == str(root)
        factory.assert_called_once_with()


def test_public_pdf_missing_shared_does_not_fallback_to_unconfigured_firefox(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("selenium.webdriver.Firefox") as legacy:
        with pytest.raises(RuntimeError, match="constancia_navegador_compartido_no_disponible"):
            _arca_browser()
        legacy.assert_not_called()


def test_public_pdf_failed_browser_creation_leaves_no_session():
    browser = MagicMock()
    browser.__enter__.side_effect = RuntimeError("synthetic profile failure")
    with patch("plugins.platforms.telegram.constancia_pdf._arca_browser", return_value=browser):
        session = ConstanciaPdfSession()
        with pytest.raises(RuntimeError, match="synthetic profile failure"):
            session.start()
        browser.__exit__.assert_called_once_with(None, None, None)
        assert session.driver is None


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


class FakeAuto:
    """Mismo contrato que LecturaAutomatica; ``lecturas`` es lo que devolvería el lector por desafío."""

    def __init__(self, lecturas=(), disponible=True):
        self.lecturas = list(lecturas)
        self.disponible = disponible
        self.desafios = self.envios = 0
        self.ultimo = None
        self.eventos = []

    def quedan(self):
        return self.disponible and self.envios < 3 and self.desafios < 8

    def leer(self, image):
        self.desafios += 1
        texto = self.lecturas.pop(0) if self.lecturas else None
        if texto is not None:
            self.envios += 1
            self.ultimo = "auto"
        self.eventos.append(("leer", image, texto))
        return texto

    def humanos_permitidos(self, pedidos):
        return pedidos < max(1, 3 - self.envios)

    def derivar(self):
        self.ultimo = "humano"
        self.eventos.append(("derivar",))

    def resultado(self, estado):
        self.eventos.append(("resultado", estado))

    def cierre(self, estado):
        self.eventos.append(("cierre", estado))

    def cerrar(self):
        self.eventos.append(("cerrar",))


def _pdf_setup():
    flow = ConstanciasFlow()
    progress = SimpleNamespace(edit_text=AsyncMock())
    bot = SimpleNamespace(send_photo=AsyncMock())
    adapter = SimpleNamespace(_bot=bot, send_document=AsyncMock(return_value=SimpleNamespace(success=True)))
    state = State("nonce", "12345", 0.0, stage="pdf_running", study_id=7,
                  contributor_id=9, job_id=44, cuit="20123456789", slug="ejemplo",
                  progress_message=progress)
    key = flow._key(55, None, 12345)
    flow.states[key] = state
    return flow, adapter, bot, state, key


def _run_auto_pdf(auto, submit_effects, renew_images=(b"png2", b"png3", b"png4", b"png5", b"png6", b"png7", b"png8")):
    async def run():
        flow, adapter, bot, state, key = _pdf_setup()
        with patch("plugins.platforms.telegram.constancia_pdf.LecturaAutomatica", return_value=auto), \
             patch.object(ConstanciaPdfSession, "start", return_value=b"png1"), \
             patch.object(ConstanciaPdfSession, "renew", side_effect=list(renew_images)) as renew, \
             patch.object(ConstanciaPdfSession, "submit", side_effect=submit_effects) as submit, \
             patch.object(ConstanciaPdfSession, "close"), \
             patch.object(flow, "_pdf_ticket", return_value="12345678-1234-1234-1234-123456789abc"), \
             patch.object(flow, "_archive_pdf", return_value={"nombre": "constancia-inscripcion-ejemplo.pdf"}):
            await flow._pdf(adapter, state, key, 55, None)
        return flow, adapter, bot, state, key, renew, submit
    return asyncio.run(run())


def test_pdf_captcha_is_solved_automatically_without_asking_the_person():
    auto = FakeAuto(["abc123"])
    flow, adapter, bot, state, key, renew, submit = _run_auto_pdf(auto, [b"%PDF-1.4\n%%EOF"])
    submit.assert_called_once_with("20123456789", "abc123")
    bot.send_photo.assert_not_awaited()
    renew.assert_not_called()
    assert ("resultado", "aceptado") in auto.eventos and ("cierre", "resuelto_solo") in auto.eventos
    assert auto.eventos[-1] == ("cerrar",)
    assert adapter.send_document.await_count == 1
    assert key not in flow.states


def test_pdf_invalid_reading_reloads_and_rejected_answer_asks_a_new_challenge():
    auto = FakeAuto([None, "abc123", "xyz789"])
    flow, adapter, bot, state, key, renew, submit = _run_auto_pdf(
        auto, [CaptchaRechazado("constancia_captcha_rechazado"), b"%PDF-1.4\n%%EOF"])
    # Lectura sin formato: otro desafío sin enviar. Rechazo de ARCA: otro desafío, nunca el mismo.
    assert [e[1] for e in auto.eventos if e[0] == "leer"] == [b"png1", b"png2", b"png3"]
    assert submit.call_args_list[0].args == ("20123456789", "abc123")
    assert submit.call_args_list[1].args == ("20123456789", "xyz789")
    assert ("resultado", "rechazado") in auto.eventos
    bot.send_photo.assert_not_awaited()
    assert adapter.send_document.await_count == 1


def test_pdf_asks_the_person_only_after_automatic_attempts_are_exhausted():
    async def run():
        auto = FakeAuto(["aaa111", "bbb222", "ccc333"])
        flow, adapter, bot, state, key = _pdf_setup()
        rejected = CaptchaRechazado("constancia_captcha_rechazado")
        with patch("plugins.platforms.telegram.constancia_pdf.LecturaAutomatica", return_value=auto), \
             patch.object(ConstanciaPdfSession, "start", return_value=b"png1"), \
             patch.object(ConstanciaPdfSession, "renew", side_effect=[b"png2", b"png3", b"png4"]), \
             patch.object(ConstanciaPdfSession, "submit", side_effect=[rejected, rejected, rejected, b"%PDF-1.4\n%%EOF"]) as submit, \
             patch.object(ConstanciaPdfSession, "close"), \
             patch.object(flow, "_pdf_ticket", return_value="12345678-1234-1234-1234-123456789abc"), \
             patch.object(flow, "_archive_pdf", return_value={"nombre": "constancia-inscripcion-ejemplo.pdf"}):
            task = asyncio.create_task(flow._pdf(adapter, state, key, 55, None))
            for _ in range(200):
                if state.stage == "captcha":
                    break
                await asyncio.sleep(.01)
            assert state.stage == "captcha"
            assert bot.send_photo.call_args.kwargs["photo"].getvalue() == b"png4"
            message = SimpleNamespace(chat_id=55, message_thread_id=None, from_user=SimpleNamespace(id=12345),
                                      text="DDD444", reply_text=AsyncMock())
            assert await flow.text(adapter, message)
            await task
        assert submit.call_count == 4 and submit.call_args.args == ("20123456789", "DDD444")
        assert ("derivar",) in auto.eventos and ("cierre", "resuelto_humano") in auto.eventos
        assert adapter.send_document.await_count == 1
    asyncio.run(run())


def test_pdf_exhausted_person_attempt_ends_without_more_requests():
    async def run():
        auto = FakeAuto(["aaa111", "bbb222", "ccc333"])
        flow, adapter, bot, state, key = _pdf_setup()
        bot.send_message = AsyncMock()
        state.progress_message.edit_text = AsyncMock()
        rejected = CaptchaRechazado("constancia_captcha_rechazado")
        with patch("plugins.platforms.telegram.constancia_pdf.LecturaAutomatica", return_value=auto), \
             patch.object(ConstanciaPdfSession, "start", return_value=b"png1"), \
             patch.object(ConstanciaPdfSession, "renew", side_effect=[b"png2", b"png3", b"png4", b"png5"]), \
             patch.object(ConstanciaPdfSession, "submit", side_effect=[rejected] * 4), \
             patch.object(ConstanciaPdfSession, "close"):
            task = asyncio.create_task(flow._pdf(adapter, state, key, 55, None))
            for _ in range(200):
                if state.stage == "captcha":
                    break
                await asyncio.sleep(.01)
            message = SimpleNamespace(chat_id=55, message_thread_id=None, from_user=SimpleNamespace(id=12345),
                                      text="DDD444", reply_text=AsyncMock())
            assert await flow.text(adapter, message)
            await task
        assert bot.send_photo.await_count == 1
        assert ("cierre", "constancia_captcha_agotado") in auto.eventos
        assert key not in flow.states
    asyncio.run(run())


def test_pdf_without_local_reader_asks_the_person_with_the_current_captcha():
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
        with patch("plugins.platforms.telegram.constancia_pdf.LecturaAutomatica",
                   return_value=FakeAuto(disponible=False)), \
             patch.object(ConstanciaPdfSession, "start", return_value=b"png"), \
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


def test_pdf_failure_falls_back_to_new_message_if_progress_cannot_be_edited():
    async def run():
        flow = ConstanciasFlow()
        progress = SimpleNamespace(edit_text=AsyncMock(side_effect=RuntimeError("edit failed")))
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        adapter = SimpleNamespace(_bot=bot)
        state = State("nonce", "12345", 0.0, stage="pdf_running", contributor_id=9,
                      job_id=44, cuit="20123456789", progress_message=progress)
        key = flow._key(55, None, 12345)
        flow.states[key] = state
        with patch("plugins.platforms.telegram.constancia_pdf.LecturaAutomatica", return_value=FakeAuto()), \
             patch.object(ConstanciaPdfSession, "start", side_effect=RuntimeError("browser failed")), \
             patch.object(ConstanciaPdfSession, "close"):
            await flow._pdf(adapter, state, key, 55, None)
        bot.send_message.assert_awaited_once()
        assert "No pude obtener" in bot.send_message.call_args.kwargs["text"]
        assert key not in flow.states
    asyncio.run(run())


def test_public_form_security_code_rejection_is_recognized():
    class Element:
        def __init__(self, driver):
            self.driver = driver

        def get_attribute(self, key):
            return "token"

        def send_keys(self, value):
            pass

        def is_displayed(self):
            return True

        def click(self):
            self.driver.current_url = "https://seti.afip.gob.ar/padron-puc-constancia-internet/MsgAction.do"

        @property
        def text(self):
            return ("ATENCIÓN: Se ha producido un error\nHa ocurrido un error durante la ejecución de la "
                    "registración.\n  Código de seguridad inválido.")

    class Driver:
        current_url = FORM

        def find_element(self, by, name):
            return Element(self)

        def find_elements(self, by, selector):
            return [Element(self)]

    session = ConstanciaPdfSession()
    session.driver = Driver()
    with pytest.raises(CaptchaRechazado):
        session.submit("20123456789", "abc123")


def test_automatic_reader_uses_shared_cycle_and_its_own_reader(tmp_path, monkeypatch):
    (tmp_path / "skills/shared/arca").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    created = []

    class Ciclo:
        def __init__(self, tipo, pantalla):
            created.append((tipo, pantalla))
            self.envios, self.ultimo = 0, None

        def quedan_automaticos(self):
            return True

        def leer(self, image, *, lector):
            assert lector is reader
            return "abc123"

    reader = MagicMock()
    fake = SimpleNamespace(Ciclo=Ciclo, _Lector=lambda: reader)
    with patch.dict(sys.modules, {"captcha_auto": fake}), patch.object(sys, "path", list(sys.path)):
        auto = LecturaAutomatica()
        assert auto.quedan() and auto.leer(b"png") == "abc123"
        auto.cerrar()
        auto.cerrar()
    assert created == [("arca", "constancia_pdf")]
    reader.cerrar.assert_called_once_with()


def test_automatic_reader_missing_falls_back_to_person(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # sin skills/shared/arca
    auto = LecturaAutomatica()
    assert not auto.quedan() and auto.humanos_permitidos(0) and not auto.humanos_permitidos(3)
    auto.derivar(); auto.resultado("aceptado"); auto.cierre("resuelto_humano"); auto.cerrar()
