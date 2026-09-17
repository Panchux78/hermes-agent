"""Client consultation naming consistent with ContaBot's current slug convention."""
import os
from pathlib import Path
import re
import shutil
import tempfile


def destination(root, slug, cuit, period_from, period_to):
    validate_identity(slug, cuit)
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
    return publish_named(source, directory, filename)


def sct_destination(root, slug, cuit, mode, start, end):
    if mode == 'empty' and start == end == '':
        # No invented fiscal year when the user explicitly chose no filter.
        validate_identity(slug, cuit)
        return Path(root)/slug/cuit/'arca'/'consultas', f'{slug}-sct-estado-cumplimiento-arca-sin-filtro.xlsx'
    if mode != 'range':
        raise ValueError('invalid_period')
    match_start = re.fullmatch(r'(\d{4})(00|0[1-9]|1[0-2])00', start)
    match_end = re.fullmatch(r'(\d{4})(0[1-9]|1[0-2])31', end)
    if not match_start or not match_end or match_start[1] != match_end[1]:
        raise ValueError('invalid_period')
    first = f'{match_start[2] if match_start[2] != "00" else "01"}/{match_start[1]}'
    last = f'{match_end[2]}/{match_end[1]}'
    directory, filename = destination(root, slug, cuit, first, last)
    reference = filename[len(f'{slug}-ccma-obligaciones-pagos-arca-'):]
    return directory, f'{slug}-sct-estado-cumplimiento-arca-{reference}'


def validate_identity(slug, cuit):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug or '') or not re.fullmatch(r'\d{11}', cuit or ''):
        raise ValueError('invalid_client_identity')


def publish_named(source, directory, filename):
    directory = Path(directory)
    suffix = Path(filename).suffix.lower()
    if Path(filename).name != filename or suffix not in {'.xlsx', '.ics'}:
        raise ValueError('invalid_filename')
    # Check before mkdir as well, so no directory is created through a symlink.
    for p in (directory,*directory.parents):
        if p.is_symlink(): raise ValueError('symlink_destination')
    # The console reads the client tree through an inherited named ACL.  Mode
    # 0700/0600 would reduce the ACL mask to zero and make a correctly archived
    # workbook invisible to the read-only service account.  Keep owner-only
    # writes while preserving read/traverse through that ACL.
    directory.mkdir(parents=True,exist_ok=True,mode=0o750)
    os.chmod(directory, 0o750)
    stem=Path(filename).stem
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(source_fd, 'rb') as inp:
        import stat
        if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
            raise ValueError('invalid_source')
        fd, temporary = tempfile.mkstemp(prefix='.fiscal-', suffix='.tmp', dir=directory)
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, 'wb') as out:
                shutil.copyfileobj(inp, out)
                out.flush(); os.fsync(out.fileno())
            for version in range(1,10000):
                target=directory/(filename if version==1 else f'{stem}-v{version:02d}{suffix}')
                try: os.link(temporary, target)
                except FileExistsError: continue
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
                return target
            raise RuntimeError('version_limit')
        finally:
            Path(temporary).unlink(missing_ok=True)
