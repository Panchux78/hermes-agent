"""#139: subclases finas de la consulta existente, sin logins ni entregas nuevas."""
from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow, RECHAZOS
from plugins.platforms.telegram.menu_buttons import menu_label, aligned_menu_label

PROVINCIAS = {
 'arba': ('ARBA','🪙','ab'),
 'tucuman': ('Tucumán','🌿','tu'),
 'santafe': ('Santa Fe','🌾','sf'),
 'cordoba': ('Córdoba','🏔️','co'),
}
PAGINAS={c+s for c in PROVINCIAS for s in ('','_consultar','_retenciones','_preparar','_presentar')}


def titulo_pagina(page):
    code,_,section=page.partition('_')
    name=PROVINCIAS[code][0]
    labels={'':'','consultar':' · Consultar','retenciones':' · Retenciones y Percepciones',
            'preparar':' · Preparar\n\nTodavía no hay funciones de preparación habilitadas en ContaBot.',
            'presentar':' · Presentar\n\nTodavía no hay presentaciones habilitadas en ContaBot.'}
    return name+labels[section]


def teclado_pagina(page,button_factory):
    code,_,section=page.partition('_'); name,_,prefix=PROVINCIAS[code]
    def button(icon,label,callback):return [button_factory(aligned_menu_label(code,icon,label),callback_data=callback)]
    if not section:
        return [button('🔎','Consultar','om:'+code+'_consultar'),button('🧾','Preparar','om:'+code+'_preparar'),button('📤','Presentar','om:'+code+'_presentar')],menu_label('‹','Organismos'),'organismos'
    if section=='consultar':return [button('📑','Retenciones y Percepciones','om:'+code+'_retenciones')],menu_label('‹',name),code
    if section=='retenciones':return [button('👤','Un cliente',prefix+':uno'),button('📋','Cartera de IIBB',prefix+':cartera')],menu_label('‹','Volver'),code+'_consultar'
    return [],menu_label('‹',name),code


def organismo_prefijo(prefix):
    return {'cd':'COMARB','ar':'AGIP',**{v[2]:v[0] for v in PROVINCIAS.values()}}[prefix]


class ProvincialDeduccionesFlow(ComarbDeduccionesFlow):
    def __init__(self,codigo,**kwargs):
        self.codigo=codigo
        name,_,prefix=PROVINCIAS[codigo]
        self.callback_prefix=prefix;self.organismo=name
        self.title='📑 Retenciones y Percepciones'
        self.back_page='om:'+codigo+'_consultar'
        self.rechazos={**RECHAZOS,
            'clave_sin_verificar': ('No hay una CIT ARBA activa y comprobada, propia o del apoderado autorizado.' if codigo=='arba' else 'Necesitás Telegram vinculado y una clave ARCA activa y comprobada, propia o del representante autorizado.')+' Revisá Accesos del panel. No se inició ningún ingreso.',
            'regimen_no_verificado':'No se pudo verificar que corresponda '+name+' en ese período. Revisá el régimen y las jurisdicciones de la ficha fiscal. No se inició ningún ingreso.'}
        self.cartera_text='Cartera de IIBB: consulta '+name+' para los clientes visibles con jurisdicción y acceso utilizables en el mes solicitado. Recibís un Excel por contribuyente; los que no pueden incluirse se informan.'
        super().__init__(**kwargs)

    def _pedir_sql(self,actor,state,period):
        operation=self.codigo+'_deducciones'
        if state.modo=='cartera':return f"SELECT console.fn_comarb_deducciones_cartera_telegram({actor},'{period}','{operation}')::text;"
        return f"SELECT console.fn_comarb_deducciones_pedir_telegram({actor},{int(state.contributor['id'])},'{period}','{operation}')::text;"

    def _fuera_de_alcance(self,state,period,organismos):
        return self.organismo+' no corresponde al régimen o a las jurisdicciones de '+state.contributor['nombre']+' en '+period+'. Revisá la ficha fiscal. No se inició ningún ingreso.'


def flows(adapter):
    """Mismo orden de despacho para callbacks y texto; sin modificar el selector."""
    existing={k:getattr(adapter,attribute,None) for k,attribute in (('cd','_comarb_deducciones_flow'),('ar','_agip_deducciones_flow'))}
    return {k:f for k,f in {**existing,**getattr(adapter,'_provinciales_iibb_flows',{})}.items() if f is not None}
