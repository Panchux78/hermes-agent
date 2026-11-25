"""Formulario público ARCA: sólo la constancia individual.

#216: el CAPTCHA del formulario se lee primero en el servidor con el lector local compartido
(``captcha_auto``, ddddocr; nada pago ni externo). Sólo cuando se agotan los intentos
automáticos se le pide a la persona por Telegram.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlparse

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

FORM = "https://seti.afip.gob.ar/padron-puc-constancia-internet/jsp/Constancia.jsp"


def _shared_arca():
    # Same installed provider as the fiscal flows: a fresh private profile,
    # intermediate cached without trust-anchor privileges, and strict TLS.
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "skills/shared/arca"
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise RuntimeError("constancia_navegador_compartido_no_disponible")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _arca_browser():
    _shared_arca()
    from arca_browser import firefox
    return firefox()


class CaptchaRechazado(RuntimeError):
    """ARCA contestó «Código de seguridad inválido»: se pide otro desafío, nunca se reenvía el mismo."""


# Límites compartidos con el ingreso a ARCA (captcha_auto): 3 envíos y 8 desafíos automáticos.
class LecturaAutomatica:
    """Ciclo automático del CAPTCHA de la constancia, con el registro común de desafíos.

    Un lector propio por solicitud, cerrado al terminar: el gateway vive mucho y no debe
    quedarse con un lector caído ni con el proceso de lectura abierto entre consultas.
    Si el lector compartido no está, la solicitud sigue como antes, por la persona.
    """

    def __init__(self):
        self.ciclo = self._lector = None
        try:
            _shared_arca()
            import captcha_auto
            self.ciclo = captcha_auto.Ciclo("arca", "constancia_pdf")
            self._lector = captcha_auto._Lector()
        except Exception as exc:  # noqa: BLE001 — sin lector se pide a la persona
            self.ciclo = self._lector = None
            self.motivo = type(exc).__name__
        else:
            self.motivo = None

    def quedan(self) -> bool:
        return self.ciclo is not None and self.ciclo.quedan_automaticos()

    def leer(self, imagen: bytes):
        return self.ciclo.leer(imagen, lector=self._lector)

    @property
    def envios(self) -> int:
        return self.ciclo.envios if self.ciclo else 0

    @property
    def ultimo(self):
        return self.ciclo.ultimo if self.ciclo else "humano"

    def humanos_permitidos(self, pedidos: int) -> bool:
        # Igual que el ingreso a ARCA: si los automáticos no alcanzaron, la persona tiene lo que falte
        # hasta 3 envíos, y al menos uno.
        return pedidos < max(1, 3 - self.envios)

    def derivar(self) -> None:
        if self.ciclo:
            self.ciclo.derivar("automaticos_agotados" if self.ciclo.envios else "sin_lectura_automatica")

    def resultado(self, estado: str) -> None:
        if self.ciclo:
            self.ciclo.resultado(estado)

    def cierre(self, estado: str) -> None:
        if self.ciclo:
            self.ciclo.cierre(estado)

    def cerrar(self) -> None:
        lector, self._lector = self._lector, None
        if lector is not None:
            lector.cerrar()


class ConstanciaPdfSession:
    def __init__(self):
        self.driver = None
        self._browser = None

    def _captcha(self) -> bytes:
        WebDriverWait(self.driver, 25).until(
            lambda d: d.find_element(By.ID, "tokenCaptcha").get_attribute("value")
            and d.find_element(By.ID, "imgCaptcha").get_attribute("src").startswith("data:image/")
        )
        return self.driver.find_element(By.ID, "imgCaptcha").screenshot_as_png

    def start(self) -> bytes:
        self._browser = _arca_browser()
        try:
            self.driver = self._browser.__enter__()
            self.driver.set_page_load_timeout(30)
            self.driver.get(FORM)
            return self._captcha()
        except BaseException:
            self.close()
            raise

    def renew(self) -> bytes:
        """Otro desafío en el mismo navegador: se vuelve a cargar el formulario, sin enviar nada."""
        if self.driver is None:
            raise RuntimeError("constancia_sesion_no_disponible")
        self.driver.get(FORM)
        return self._captcha()

    def submit(self, cuit: str, solution: str) -> bytes:
        if not re.fullmatch(r"[0-9]{11}", cuit) or not re.fullmatch(r"[A-Za-z0-9]{6}", solution):
            raise ValueError("constancia_solicitud_invalida")
        driver = self.driver
        if driver is None or driver.current_url != FORM:
            raise RuntimeError("constancia_sesion_no_disponible")
        if not driver.find_element(By.ID, "tokenCaptcha").get_attribute("value"):
            raise RuntimeError("constancia_captcha_vencido")
        driver.find_element(By.ID, "cuit").send_keys(cuit)
        driver.find_element(By.ID, "token").send_keys(solution)
        buttons = driver.find_elements(By.CSS_SELECTOR, "button[onclick='ejecutarRest(true)']")
        visible = [button for button in buttons if button.is_displayed()]
        if len(visible) != 1:
            raise RuntimeError("constancia_control_ambiguo")
        visible[0].click()
        # Esperar el texto de la página nueva: justo después del cambio de dirección el cuerpo
        # todavía puede no existir (medido en la #216).
        body = WebDriverWait(driver, 30).until(
            lambda d: d.current_url != FORM and d.find_element(By.TAG_NAME, "body").text)
        url = urlparse(driver.current_url)
        if (url.scheme != "https" or url.hostname != "seti.afip.gob.ar"
                or not url.path.startswith("/padron-puc-constancia-internet/")):
            raise RuntimeError("constancia_redireccion_inesperada")
        if (url.path.endswith("/MsgAction.do")
                and re.search(r"c[oó]digo de seguridad inv[aá]lido", body, re.IGNORECASE)):
            raise CaptchaRechazado("constancia_captcha_rechazado")
        identifiers = {re.sub(r"[^0-9]", "", value) for value in
                       re.findall(r"(?<![0-9])[0-9]{2}-?[0-9]{8}-?[0-9](?![0-9])", body)}
        normalized = unicodedata.normalize("NFKD", body.upper()).encode("ascii", "ignore").decode("ascii")
        if cuit not in identifiers or not any(
            title in normalized for title in ("CONSTANCIA DE INSCRIPCION", "CONSTANCIA DE OPCION")
        ):
            raise RuntimeError("constancia_identidad_no_verificada")
        document = base64.b64decode(driver.print_page(), validate=True)
        if not document.startswith(b"%PDF-") or b"%%EOF" not in document[-2048:] or len(document) > 20 * 1024 * 1024:
            raise RuntimeError("constancia_pdf_invalido")
        return document

    def close(self) -> None:
        browser, driver = self._browser, self.driver
        self._browser = None
        self.driver = None
        if browser is not None:
            browser.__exit__(None, None, None)
        elif driver is not None:
            driver.quit()
