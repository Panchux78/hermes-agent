"""Client consultation naming consistent with ContaBot's current slug convention."""
import os
from pathlib import Path
import re
import shutil


def destination(root, slug, cuit, period_from, period_to):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug or '') or not re.fullmatch(r'\d{11}', cuit or ''):
        raise ValueError('invalid_client_identity')
    for period in (period_from, period_to):
        if not re.fullmatch(r'(0[1-9]|1[0-2])/\d{4}', period): raise ValueError('invalid_period')
    fm,fy=period_from.split('/');tm,ty=period_to.split('/')
    if (fy,fm) > (ty,tm): raise ValueError('reversed_period')
    if period_from == period_to: reference,folder=f'{fy}-{fm}',fm
    elif fy==ty and fm=='01' and tm=='12': reference,folder=fy,'anual'
    else: reference,folder=f'{fy}{fm}-{ty}{tm}','anual'
    return Path(root)/slug/cuit/'arca'/fy/folder/'consultas',f'{slug}-ccma-obligaciones-pagos-arca-{reference}.xlsx'


def publish(source, root, slug, cuit, period_from, period_to):
    directory,filename=destination(root,slug,cuit,period_from,period_to)
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Reject symlink components before allocating a new client artifact.
    for p in (directory,*directory.parents):
        if p.is_symlink(): raise ValueError('symlink_destination')
    stem=Path(filename).stem
    for version in range(1,10000):
        target=directory/(filename if version==1 else f'{stem}-v{version:02d}.xlsx')
        try: fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError: continue
        try:
            with os.fdopen(fd,'wb') as out, Path(source).open('rb') as inp:
                shutil.copyfileobj(inp,out)
                out.flush();os.fsync(out.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target
    raise RuntimeError('version_limit')
