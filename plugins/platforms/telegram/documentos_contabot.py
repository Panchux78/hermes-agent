"""Puerta Hermes → módulo único de documentos de ContaBot (Ágora #115).

Hermes no arma rutas del árbol de clientes: entrega el archivo producido a
``documentos_cliente.py`` de la release fija de ContaBot (igual que
``bot_runs.py``), que decide ruta, versión y cuota y lo cataloga. Un fallo
nunca se informa como guardado.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from plugins.platforms.telegram.bot_run_history import _DEFAULT_PROJECT

# Texto de ContaBot (documentos_cliente.MENSAJE_CUOTA); se usa si la salida no lo trae.
MENSAJE_CUOTA = ("El estudio no tiene espacio suficiente. Liberá archivos desde Documentos "
                 "o pedí ampliar la cuota.")
CODIGO_CUOTA = "cuota_insuficiente"
_TIMEOUT_SEGUNDOS = 60
_SCRIPT = "skills/accounting/pdf-contable-router/scripts/documentos_cliente.py"


class DocumentoNoGuardado(RuntimeError):
    """El módulo de documentos no confirmó el guardado; ``codigo`` es saneado."""

    def __init__(self, codigo: str) -> None:
        super().__init__(codigo)
        self.codigo = codigo


class CuotaInsuficiente(DocumentoNoGuardado):
    """El estudio no tiene espacio; ``mensaje`` es el texto para el contador."""

    def __init__(self, mensaje: str | None = None) -> None:
        super().__init__(CODIGO_CUOTA)
        self.mensaje = mensaje or MENSAJE_CUOTA


def _proyecto() -> Path:
    return Path(os.getenv("CONTA_PDF_ROUTER_PROJECT_DIR", str(_DEFAULT_PROJECT)))


def _script() -> Path:
    path = _proyecto() / _SCRIPT
    if path.is_symlink() or not path.is_file():
        raise DocumentoNoGuardado("documentos_no_disponible")
    return path


def _ultimo_json(raw: bytes) -> dict[str, Any] | None:
    for linea in reversed(raw.decode("utf-8", errors="replace").splitlines()):
        linea = linea.strip()
        if not linea.startswith("{"):
            continue
        try:
            datos = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if isinstance(datos, dict):
            return datos
    return None


async def _llamar(*argumentos: str) -> dict[str, Any]:
    script = _script()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), *argumentos,
        cwd=str(_proyecto()), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SEGUNDOS)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        raise DocumentoNoGuardado("documentos_tiempo_agotado") from None
    datos = _ultimo_json(raw)
    if datos is None:
        raise DocumentoNoGuardado("documentos_respuesta_invalida")
    if datos.get("codigo") == CODIGO_CUOTA:
        mensaje = datos.get("mensaje")
        raise CuotaInsuficiente(mensaje if isinstance(mensaje, str) and mensaje.strip() else None)
    if proc.returncode != 0 or datos.get("status") != "OK":
        codigo = datos.get("codigo")
        raise DocumentoNoGuardado(codigo if isinstance(codigo, str) and codigo else "documentos_error")
    return datos


async def guardar(origen: Path, destino: dict[str, Any]) -> dict[str, Any]:
    """Guarda ``origen`` con el destino del contrato; devuelve id_archivo, ruta, path."""
    datos = await _llamar(
        "guardar", "--origen", str(origen),
        "--destino", json.dumps(destino, ensure_ascii=False, separators=(",", ":")),
    )
    ruta = datos.get("ruta")
    if (not isinstance(datos.get("id_archivo"), int) or not isinstance(ruta, str)
            or not ruta.startswith("estudios/") or ".." in Path(ruta).parts):
        raise DocumentoNoGuardado("documentos_respuesta_invalida")
    return datos


async def espacio(id_contribuyente: int, bytes_previstos: int) -> None:
    """Chequeo previo de cuota; lanza CuotaInsuficiente sin iniciar efectos."""
    await _llamar("espacio", "--contribuyente", str(int(id_contribuyente)),
                  "--bytes", str(int(bytes_previstos)))


def mensaje_si_cuota(texto_o_payload: Any) -> str | None:
    """Mensaje de cuota si una salida de ContaBot informa ``cuota_insuficiente``.

    Defensivo: acepta el dict de resultado, bytes o texto, y busca el código en
    cualquier valor (``codigo``, ``reason``, ``motivo``, ``error_code``, casos…).
    """
    if texto_o_payload is None:
        return None
    if isinstance(texto_o_payload, (bytes, bytearray)):
        texto_o_payload = bytes(texto_o_payload).decode("utf-8", errors="replace")
    if isinstance(texto_o_payload, str):
        texto = texto_o_payload
    else:
        try:
            texto = json.dumps(texto_o_payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            texto = str(texto_o_payload)
    if CODIGO_CUOTA not in texto.lower():
        return None
    if isinstance(texto_o_payload, dict):
        mensaje = texto_o_payload.get("mensaje")
        if (isinstance(mensaje, str) and mensaje.strip()
                and str(texto_o_payload.get("codigo", "")).lower() == CODIGO_CUOTA):
            return mensaje
    return MENSAJE_CUOTA
