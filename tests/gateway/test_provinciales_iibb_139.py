from pathlib import Path
import os,re
from types import SimpleNamespace
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.provinciales_iibb import PROVINCIAS, ProvincialDeduccionesFlow
from plugins.platforms.telegram.comarb_deducciones_flow import State, resumen
import pytest
CHECKOUT=os.environ.get('CONTABOT_CHECKOUT')


@pytest.fixture(autouse=True)
def botones(monkeypatch):
    import plugins.platforms.telegram.adapter as a
    monkeypatch.setattr(a,'InlineKeyboardButton',lambda text,callback_data:SimpleNamespace(text=text,callback_data=callback_data))
    monkeypatch.setattr(a,'InlineKeyboardMarkup',lambda rows:SimpleNamespace(inline_keyboard=rows))


def test_organismos_y_orden_vienen_del_documento_canonico():
    if not CHECKOUT:raise RuntimeError('Indicá CONTABOT_CHECKOUT para el contrato de menú')
    root=Path(CHECKOUT)
    block=(root/'documentos/workspace/menu-telegram-canonico.md').read_text().split('## Estructura funcional vigente')[1].split('├── Bancos')[0]
    expected=re.findall(r'^│   [├└]── \S+ (.+)$',block,re.M)
    keyboard=TelegramAdapter._menu_panel_keyboard('organismos').inline_keyboard
    actual=[r[0].text.removesuffix('\u200a\u200a').split('  ',1)[1] for r in keyboard[:-1]]
    assert actual==expected


def test_cada_provincia_usa_selector_cola_y_navegacion_comunes(monkeypatch):
    import plugins.platforms.telegram.adapter as a
    monkeypatch.setattr(a,'InlineKeyboardButton',lambda text,callback_data:SimpleNamespace(text=text,callback_data=callback_data))
    monkeypatch.setattr(a,'InlineKeyboardMarkup',lambda rows:SimpleNamespace(inline_keyboard=rows))
    for code,(name,_,prefix) in PROVINCIAS.items():
        flow=ProvincialDeduccionesFlow(code)
        state=State(user_id='1',nonce='n',stage='periodo',modo='uno',contributor={'id':99,'nombre':'Sintético'})
        assert flow.callback_prefix==prefix
        assert code+'_deducciones' in flow._pedir_sql(1,state,'09/2026')
        assert '99' in flow._pedir_sql(1,state,'09/2026')
        for page in (code,code+'_consultar',code+'_retenciones',code+'_preparar',code+'_presentar'):
            assert TelegramAdapter._menu_panel_title(page).startswith(name)
            rows=TelegramAdapter._menu_panel_keyboard(page).inline_keyboard
            assert all(len(row)==1 for row in rows[:-1])
            assert [b.callback_data for b in rows[-1]][-1]=='om:close'
        regime=name+' · Retenciones'
        text=resumen({'nombre_legal':'Sintético','periodo':'09/2026','estado':'terminada','resultado':{'operacion':code+'_deducciones','estados':{regime:{'estado':'con_datos','filas':2,'importe':'74.76'}}}})
        assert regime in text and '$ 74,76' in text
        assert 'SIRCREB' not in text and 'SIRCUPA' not in text
