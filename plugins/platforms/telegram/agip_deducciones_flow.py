"""AGIP Gestión AR: mismo selector, cola, progreso, cancelación y entrega de COMARB."""
from plugins.platforms.telegram.comarb_deducciones_flow import ComarbDeduccionesFlow, RECHAZOS


class AgipDeduccionesFlow(ComarbDeduccionesFlow):
    callback_prefix = 'ar'
    title = '📑 Retenciones y Percepciones'
    organismo = 'AGIP'
    rechazos = {**{key:text.replace('COMARB','AGIP') for key,text in RECHAZOS.items()},
        'clave_sin_verificar':'No hay una Clave Ciudad activa y utilizable, propia o del representante autorizado. Revisá Accesos del panel. miBA todavía no está soportado. No se inició ningún ingreso.',
        'regimen_no_verificado':'No se pudo verificar que corresponda AGIP en ese período. Revisá el régimen y las jurisdicciones en la ficha fiscal. No se inició ningún ingreso.'}
    back_page = 'om:agip_consultar'
    cartera_text = ('Cartera de IIBB: clientes visibles con Clave Ciudad utilizable, propia o del representante, '
        'locales mensuales de CABA o Convenio con CABA en el mes solicitado. Sólo consulta AGIP; '
        'no ingresa a ARCA. Recibís un Excel por contribuyente. miBA todavía no está soportado.')

    def _pedir_sql(self, actor, state, period):
        if state.modo=='cartera':
            return f"SELECT console.fn_comarb_deducciones_cartera_telegram({actor},'{period}','agip_deducciones')::text;"
        return (f"SELECT console.fn_comarb_deducciones_pedir_telegram({actor},"
                f"{int(state.contributor['id'])},'{period}','agip_deducciones')::text;")

    def _fuera_de_alcance(self, state, period, organismos):
        return (f"AGIP no corresponde a las jurisdicciones de {state.contributor['nombre']} en {period}. "
                "Esta consulta admite locales mensuales de CABA y Convenio con CABA. No se inició ningún ingreso.")
