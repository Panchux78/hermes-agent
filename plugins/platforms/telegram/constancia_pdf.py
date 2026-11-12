"""Formulario público ARCA: sólo la constancia individual, con CAPTCHA humano."""

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


def _arca_browser():
    # Same installed provider as the fiscal flows: a fresh private profile,
    # intermediate cached without trust-anchor privileges, and strict TLS.
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "skills/shared/arca"
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise RuntimeError("constancia_navegador_compartido_no_disponible")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from arca_browser import firefox
    return firefox()


class ConstanciaPdfSession:
    def __init__(self):
        self.driver = None
        self._browser = None

    def start(self) -> bytes:
        self._browser = _arca_browser()
        try:
            self.driver = self._browser.__enter__()
            self.driver.set_page_load_timeout(30)
            self.driver.get(FORM)
            WebDriverWait(self.driver, 25).until(
                lambda d: d.find_element(By.ID, "tokenCaptcha").get_attribute("value")
                and d.find_element(By.ID, "imgCaptcha").get_attribute("src").startswith("data:image/")
            )
            return self.driver.find_element(By.ID, "imgCaptcha").screenshot_as_png
        except BaseException:
            self.close()
            raise

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
        WebDriverWait(driver, 30).until(lambda d: d.current_url != FORM)
        url = urlparse(driver.current_url)
        if (url.scheme != "https" or url.hostname != "seti.afip.gob.ar"
                or not url.path.startswith("/padron-puc-constancia-internet/")):
            raise RuntimeError("constancia_redireccion_inesperada")
        body = driver.find_element(By.TAG_NAME, "body").text
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
