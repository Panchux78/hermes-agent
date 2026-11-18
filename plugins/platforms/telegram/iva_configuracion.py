"""Resumen de configuración192. La decisión y el preflight están en la base."""
def resumen(config):
    faltan=config.get('faltantes')
    if not isinstance(faltan,list):raise ValueError('configuracion_iva_no_verificada')
    if faltan:return 'Completá Ficha › Impuestos › IVA: '+', '.join(faltan)+'. No se iniciará ARCA.'
    v=config['valor']
    if any(type(v.get(k)) is not bool for k in ('prorrateo','importador','exportador')):raise ValueError('configuracion_iva_no_verificada')
    modos={'directa':'asignación directa','global':'global','directa_y_global':'asignación directa y global'}
    tipos={'bienes':'bienes','servicios':'servicios','ambos':'bienes y servicios'}
    return ('Prorrateo: '+('sí · '+modos[v['modalidad']] if v['prorrateo'] else 'no')+
            '\nImportador: '+('sí · '+tipos[v['tipo_importacion']] if v['importador'] else 'no')+
            '\nExportador: '+('sí' if v['exportador'] else 'no')+' (dato del estudio; sin casilla en ARCA).')


def consulta(actor,ids,periodo):
    import re
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',periodo) or not ids:raise ValueError('preflight_iva_invalido')
    return f"SELECT console.fn_iva_preflight_telegram({int(actor)},ARRAY[{','.join(str(int(i)) for i in ids)}]::bigint[],date '{periodo}-01')::text;"


def dividir(texto,limite=3500):
    """Telegram cuenta unidades UTF16; no truncar clientes ni inundar el chat."""
    result=[];part='';size=0
    for line in texto.splitlines(keepends=True):
        n=len(line.encode('utf-16-le'))//2
        if n<=limite:
            if size+n>limite:result.append(part.rstrip());part='';size=0
            part+=line;size+=n
        else:
            for char in line:
                n=len(char.encode('utf-16-le'))//2
                if size+n>limite:result.append(part.rstrip());part='';size=0
                part+=char;size+=n
    if part.strip():result.append(part.rstrip())
    return result


def mensajes_lote(rows,periodo):
    return dividir(f'Configuración de IVA · {periodo}\n\n'+'\n\n'.join(
        f"• {r['nombre']}\n{resumen(r['configuracion'])}" for r in rows))
