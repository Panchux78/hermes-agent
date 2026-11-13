"""Gramática SIFERE por pedido de Sol; ejemplos sintéticos y reloj fijo."""
import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
MODULE = 'plugins.platforms.telegram.comarb_ddjj_periodos'


class PeriodosTests(unittest.TestCase):
    def test_coma_enumera_periodos_aislados_y_guion_es_rango(self):
        self.assertIsNotNone(importlib.util.find_spec(MODULE), 'Falta la gramática de DDJJ presentadas')
        from plugins.platforms.telegram.comarb_ddjj_periodos import parse_periodos
        self.assertEqual(parse_periodos('01/2025, 08/2025', 'lote', today=date(2026,10,28)), ['01/2025','08/2025'])
        self.assertEqual(parse_periodos('01/2025 - 08/2025', 'lote', today=date(2026,10,28)),
                         [f'{m:02d}/2025' for m in range(1,9)])


    def test_hasta_incluye_cada_mes_y_cruza_anio(self):
        from plugins.platforms.telegram.comarb_ddjj_periodos import parse_periodos
        self.assertEqual(parse_periodos('11/2024 hasta 02/2025', 'lote', today=date(2026,10,28)),
                         ['11/2024','12/2024','01/2025','02/2025'])


if __name__ == '__main__':
    unittest.main()
