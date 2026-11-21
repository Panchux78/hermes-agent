"""Resumen de configuración192. La decisión y el preflight están en la base.

Criterios de la contadora del 21/11/2026: todo arranca en «No», sin rótulo «por
defecto»; importación de bienes y de servicios por separado, y las otras opciones
de Datos Iniciales de ARCA (TurIVA, bienes usados). Exportador: dato del cliente."""
def resumen(config):
    faltan=config.get('faltantes')
    if not isinstance(faltan,list):raise ValueError('configuracion_iva_no_verificada')
    if faltan:return 'Completá Ficha › Impuestos › IVA: '+', '.join(faltan)+'. No se iniciará ARCA.'
    v=config['valor']
    if any(type(v.get(k)) is not bool for k in ('prorrateo','importador','exportador')):raise ValueError('configuracion_iva_no_verificada')
    if any(v.get(k) is not None and type(v.get(k)) is not bool for k in ('turiva','bienes_usados')):raise ValueError('configuracion_iva_no_verificada')
    si=lambda b:'sí' if b else 'no'
    modos={'directa':'asignación directa','global':'global','directa_y_global':'asignación directa y global'}
    tipo=v.get('tipo_importacion') if v['importador'] else None
    return ('Prorrateo: '+('sí · '+modos[v['modalidad']] if v['prorrateo'] else 'no')+
            '\nImportación de bienes: '+si(tipo in ('bienes','ambos'))+
            '\nImportación de servicios: '+si(tipo in ('servicios','ambos'))+
            '\nTurIVA: '+si(v.get('turiva'))+' · Bienes usados: '+si(v.get('bienes_usados'))+
            '\nExportador: '+si(v['exportador'])+' (dato del cliente; sin casilla en ARCA).')


def version(config):
    """Versión a revalidar en el ejecutor; 0 = sin ficha guardada, todo en «No» por defecto."""
    vid=config.get('id_dato')
    if type(vid) is int and vid>0:return vid
    if vid is None and sorted(config.get('por_defecto') or [])==['exportador','importador','prorrateo']:return 0
    raise ValueError('configuracion_sin_version')


def consulta(actor,ids,periodo):
    import re
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',periodo) or not ids:raise ValueError('preflight_iva_invalido')
    return f"SELECT console.fn_iva_preflight_telegram_v2({int(actor)},ARRAY[{','.join(str(int(i)) for i in ids)}]::bigint[],date '{periodo}-01')::text;"


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
