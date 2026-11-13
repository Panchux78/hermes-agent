#!/usr/bin/env python3
"""Gate estático solicitado en #168, separado de los tests de comportamiento.

Detecta nuevas ayudas de entrada y parsers paralelos; los tests ejercitan los
handlers reales y la garantía de no ejecutar antes de confirmar.
"""
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / 'plugins/platforms/telegram'
ENTRY_POINTS = {
    'portal_iva_flow.py': {'text', '_select'},
    'portal_iva_ddjj_flow.py': {'receive_period'},
    'mis_retenciones_flow.py': {'receive_period'},
    'fiscal_query_flow.py': {'_request_period', '_handle_workflow_menu_text'},
    'agip_ddjj_flow.py': {'text', '_select'},
    'comarb_ddjj_flow.py': {'text', '_ask_period'},
    'comarb_deducciones_flow.py': {'text', '_ask_period', 'callback'},
}
# Convierte el período técnico recibido de la base para una celda de Excel;
# no recibe texto del usuario ni pide un período.
OUTPUT_ONLY = {('vencimientos_export.py', '_periodo'), ('agip_ddjj_flow.py', 'display_period')}


def inspect():
    errors = [f'{name}: falta el flujo registrado' for name in ENTRY_POINTS if not (ROOT / name).is_file()]
    for path in sorted(ROOT.glob('*.py')):
        tree = ast.parse(path.read_text(), filename=str(path))
        if path.name == 'period_input.py':
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r'Ingresá.*(?:MM/AAAA|primer período|último período)', node.value):
                    errors.append(f'{path.name}:{node.lineno}: ayuda de entrada fuera de la pieza común')
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if ('period' in node.name and any(isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == 're' and c.func.attr in ('fullmatch', 'match', 'compile')
                    for c in ast.walk(node)) and (path.name, node.name) not in OUTPUT_ONLY):
                    errors.append(f'{path.name}:{node.lineno}: parser paralelo: {node.name}')
        for name in ENTRY_POINTS.get(path.name, set()):
            functions = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name]
            if not functions or not any(isinstance(n, ast.Name) and n.id in ('SPECS','propose_period','receive_period')
                                       for f in functions for n in ast.walk(f)):
                errors.append(f'{path.name}: entrada no conectada: {name}')
    return errors


if __name__ == '__main__':
    failures = inspect()
    for failure in failures:
        print(failure)
    print('Gate de entrada única de período: ' + ('FAIL' if failures else 'PASS'))
    raise SystemExit(bool(failures))
