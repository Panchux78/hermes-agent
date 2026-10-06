"""CLI falso de ``documentos_cliente.py`` para los flujos de Telegram (Ágora #115).

Se instala como release de ContaBot en un directorio temporal: Hermes lo corre
como subprocess real (igual que el de producción), pero sin base ni árbol real.
Guarda en una raíz temporal con la forma ``estudios/1/<id>/<AAAA>/<MM|anual>/<sección>/``
y registra cada llamada en un JSONL.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_SCRIPT = r'''
import json, os, shutil, sys
from pathlib import Path
log = Path(os.environ["FAKE_DOCUMENTOS_LOG"])
raiz = Path(os.environ["FAKE_DOCUMENTOS_RAIZ"])
cuota = os.environ.get("FAKE_DOCUMENTOS_CUOTA", "")
args = sys.argv[1:]
comando = args[0]
valores = dict(zip(args[1::2], args[2::2]))
with log.open("a") as stream:
    stream.write(json.dumps({"comando": comando, **valores}) + "\n")
def error(codigo, mensaje):
    print(json.dumps({"status": "ERROR", "codigo": codigo, "mensaje": mensaje, "detalle": {}}))
    sys.exit(2)
if cuota == comando:
    error("cuota_insuficiente", "El estudio no tiene espacio suficiente. Liberá archivos desde Documentos o pedí ampliar la cuota.")
if os.environ.get("FAKE_DOCUMENTOS_FALLA") == comando:
    error("base_no_disponible", "base_no_disponible")
if comando == "espacio":
    print(json.dumps({"status": "OK"}))
    sys.exit(0)
destino = json.loads(valores["--destino"])
carpeta = "anual" if destino["mes"] is None else f'{destino["mes"]:02d}'
relativa = Path("estudios/1") / str(destino["id_contribuyente"]) / str(destino["anio"]) / carpeta / destino["seccion"]
(raiz / relativa).mkdir(parents=True, exist_ok=True)
for version in range(1, 100):
    nombre = destino["base"] + ("" if version == 1 else f"-v{version:02d}") + "." + destino["ext"]
    if not (raiz / relativa / nombre).exists():
        break
shutil.copyfile(valores["--origen"], raiz / relativa / nombre)
print(json.dumps({"status": "OK", "id_archivo": version, "ruta": str(relativa / nombre),
                  "path": str(raiz / relativa / nombre), "bytes": 1, "sha256": "0" * 64}))
'''


@dataclass
class Documentos:
    raiz: Path
    log: Path

    def llamadas(self, comando: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        filas = [json.loads(linea) for linea in self.log.read_text().splitlines() if linea.strip()]
        return [f for f in filas if comando is None or f["comando"] == comando]

    def destinos(self) -> list[dict]:
        return [json.loads(f["--destino"]) for f in self.llamadas("guardar")]

    def guardados(self) -> list[Path]:
        return sorted(p for p in (self.raiz / "estudios").rglob("*") if p.is_file()) \
            if (self.raiz / "estudios").exists() else []


def instalar(monkeypatch, tmp_path: Path, *, cuota: str = "", falla: str = "") -> Documentos:
    """``cuota``/``falla``: "espacio" o "guardar" para que ese subcomando falle."""
    proyecto = tmp_path / "contabot-falso"
    script = proyecto / "skills/accounting/pdf-contable-router/scripts/documentos_cliente.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_SCRIPT)
    raiz = tmp_path / "clientes-documentos"
    raiz.mkdir(exist_ok=True)
    log = tmp_path / "documentos-llamadas.jsonl"
    monkeypatch.setenv("CONTA_PDF_ROUTER_PROJECT_DIR", str(proyecto))
    monkeypatch.setenv("FAKE_DOCUMENTOS_RAIZ", str(raiz))
    monkeypatch.setenv("FAKE_DOCUMENTOS_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCUMENTOS_CUOTA", cuota)
    monkeypatch.setenv("FAKE_DOCUMENTOS_FALLA", falla)
    return Documentos(raiz, log)
