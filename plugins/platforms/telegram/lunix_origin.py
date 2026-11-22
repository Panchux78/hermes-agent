"""Vincula un envío de QA con su update real; no otorga permisos de operación."""
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import time

PROCESS = 'control_diario_lunix'
TTL = 30_000_000_000


def root():
    return Path.home()/'.local/state/contabot-lunix-qa/origen'


def valid_root(path):
    try:
        meta=path.lstat()
        return (path.resolve()==path and stat.S_ISDIR(meta.st_mode) and meta.st_uid==os.geteuid()
                and stat.S_IMODE(meta.st_mode)==0o700)
    except OSError:
        return False


def fingerprint(actor, kind, mid, payload):
    if (not re.fullmatch(r'[1-9][0-9]{3,19}',str(actor)) or kind not in {'callback','mensaje'}
        or (kind=='callback' and (type(mid) is not int or mid<=0))
        or (kind=='mensaje' and mid is not None) or not isinstance(payload,bytes) or not 0<len(payload)<=512):
        return None
    # En un privado, Telegram asigna un message_id distinto a cada cuenta.
    # El cliente sí exige su ID al pulsar el botón; para enlazar ambos
    # extremos se usa el wire opaco completo (con nonce cuando corresponde).
    return sha256(f'v2:{actor}:{kind}:'.encode()+payload).hexdigest()


def arm(actor, kind, mid, payload, *, directory=None):
    directory = root() if directory is None else directory
    key=fingerprint(actor,kind,mid,payload)
    if key is None or not valid_root(directory):
        raise ValueError('reserva_origen_no_verificada')
    path=directory/key
    try:
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as old:
            meta=os.fstat(old.fileno())
            raw=old.read(22)
            if (not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode)!=0o600 or meta.st_nlink!=1
                    or meta.st_uid!=os.geteuid() or not re.fullmatch(rb'[0-9]{1,20}\n',raw) or int(raw)>=time.monotonic_ns()):
                raise ValueError('reserva_origen_en_uso')
        path.unlink()
    except FileNotFoundError:
        pass
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as marker:
        marker.write(f'{time.monotonic_ns()+TTL}\n'.encode())
    return path


def claim(actor, kind, mid, payload, *, directory=None):
    directory = root() if directory is None else directory
    key=fingerprint(actor,kind,mid,payload)
    if key is None or not valid_root(directory):
        return False
    path=directory/key
    try:
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as marker:
            meta=os.fstat(marker.fileno())
            raw=marker.read(22)
            if (not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode)!=0o600 or meta.st_nlink!=1
                    or meta.st_uid!=os.geteuid() or not re.fullmatch(rb'[0-9]{1,20}\n',raw)):
                return False
            before=time.monotonic_ns()
            if not 0<int(raw)-before<=TTL or path.lstat().st_ino!=meta.st_ino:
                return False
            path.unlink()
            return True
    except (OSError,ValueError):
        return False
