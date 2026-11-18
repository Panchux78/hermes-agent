"""#192: mensajes del preflight compartido, sin ARCA ni credenciales."""
import pytest
from plugins.platforms.telegram.iva_configuracion import consulta,resumen
from plugins.platforms.telegram.preparar_lote_flow import PrepararLoteFlow

def test_ausencia_no_es_no():
    text=resumen(dict(faltantes=['modalidad de prorrateo']))
    assert 'Ficha › Impuestos › IVA' in text and 'No se iniciará ARCA' in text

def test_no_inventa_exportador_arca():
    text=resumen(dict(faltantes=[],valor=dict(prorrateo=True,modalidad='global',importador=True,tipo_importacion='bienes',exportador=False)))
    assert 'sí · global' in text and 'sí · bienes' in text and 'sin casilla en ARCA' in text

def test_sql_tipeado_y_periodo_interno():
    assert "date '2026-09-01'" in consulta(99,[1,2],'2026-09')
    with pytest.raises(ValueError):consulta(99,[1],'09/2026')

def test_no_pierde_motivo_configuracion():
    text='Completá Ficha › Impuestos › IVA: importador.'
    assert PrepararLoteFlow._no_incluidos(None,[dict(motivo=text)])[0]['motivo']==text
