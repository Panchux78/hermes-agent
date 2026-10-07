"""Bounded, local preflight in the fiscal Python and the selected Node probe."""
import asyncio
import logging
import os
from pathlib import Path
import time
import uuid

from plugins.platforms.telegram.fiscal_execution import terminate_owned_group
from plugins.platforms.telegram.fiscal_credentials import require_fiscal_database

logger = logging.getLogger(__name__)
# A real cold start and shutdown took 31.83 s on the production host. Keep a
# finite budget, separate from the lightweight Python dependency check.
BROWSER_PREFLIGHT_TIMEOUT = 90
PREFLIGHT_OUTPUT_LIMIT = 32768

PYTHON_CHECK = (
    "import openpyxl, selenium; from importlib.metadata import version; "
    "assert version('openpyxl') == '3.1.5'; "
    "assert version('selenium') == '4.48.0'; print('fiscal_python_ready')"
)


def browser_environment(home, *, python=None):
    environment = {key: os.environ[key] for key in ('HOME', 'PATH', 'LANG') if key in os.environ}
    environment.setdefault('HOME', str(Path.home()))
    environment.setdefault('PATH', '/usr/bin:/bin')
    environment.setdefault('LANG', 'C.UTF-8')
    environment['HERMES_HOME'] = str(home)
    if python is not None:
        environment['FISCAL_RUNTIME_PYTHON'] = str(python)
    # Playwright's own optional setting must agree in preflight and consultation.
    if 'PLAYWRIGHT_BROWSERS_PATH' in os.environ:
        value = os.environ['PLAYWRIGHT_BROWSERS_PATH']
        if value != '0' and (not Path(value).is_absolute() or '..' in Path(value).parts):
            raise RuntimeError('fiscal_browser_missing')
        environment['PLAYWRIGHT_BROWSERS_PATH'] = value
    return environment


async def _read_bounded(stream):
    chunks = bytearray()
    overflow = False
    while chunk := await stream.read(4096):
        remaining = PREFLIGHT_OUTPUT_LIMIT - len(chunks)
        chunks.extend(chunk[:remaining])
        overflow |= len(chunk) > remaining
    return bytes(chunks), overflow


async def _check(command, environment, expected, *, timeout=25, component='python'):
    process = None
    started = time.monotonic()
    reference = uuid.uuid4().hex[:12]
    failure = None
    error_kind = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command, env=environment, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        output, errors, _ = await asyncio.wait_for(asyncio.gather(
            _read_bounded(process.stdout), _read_bounded(process.stderr), process.wait()), timeout)
        stdout, output_overflow = output
        stderr, errors_overflow = errors
        if process.returncode == 0 and not output_overflow and not errors_overflow and stdout.strip() == expected:
            return True
        failure = 'fiscal_runtime_protocol' if process.returncode == 0 else 'fiscal_python_missing'
        if component == 'browser' and process.returncode != 0:
            failure = ('fiscal_browser_missing' if b'RuntimeError: fiscal_browser_missing' in stderr
                       else 'fiscal_browser_start_failed')
        # Never persist raw output, exception messages, environment or argv.
        error_kind = next((name for name in (
            'SessionNotCreatedException', 'WebDriverException', 'TimeoutException',
            'ModuleNotFoundError', 'PermissionError', 'FileNotFoundError', 'RuntimeError')
            if (name + ':').encode() in stderr), 'unclassified')
    except asyncio.TimeoutError:
        failure = f'fiscal_{component}_timeout'
        error_kind = 'TimeoutError'
    except OSError as error:
        failure = 'fiscal_runtime_missing'
        error_kind = type(error).__name__
    finally:
        await terminate_owned_group(process)
        if failure:
            logger.warning('FISCAL_PREFLIGHT reference=%s component=%s code=%s elapsed_ms=%d returncode=%s error_kind=%s',
                           reference, component, failure, int((time.monotonic() - started) * 1000),
                           process.returncode if process else None, error_kind)
    if failure == 'fiscal_python_missing':
        return False
    raise RuntimeError(failure)


async def require_fiscal_runtime(python, node, probe, home, *, canonical=True):
    if (not python or not Path(python).is_absolute() or not Path(python).is_file()
            or not os.access(python, os.X_OK) or not node or not probe.is_file()):
        raise RuntimeError('fiscal_runtime_missing')
    if canonical:
        await require_fiscal_database()
    environment = browser_environment(home, python=python)
    if not await _check([str(python), '-I', '-B', '-c', PYTHON_CHECK], environment, b'fiscal_python_ready'):
        raise RuntimeError('fiscal_python_missing')
    await _check([node, str(probe), '--preflight'], environment, b'result=fiscal_runtime_ready',
                 timeout=BROWSER_PREFLIGHT_TIMEOUT, component='browser')


def unavailable_message(operation, code):
    detail = {
        'fiscal_database_permissions': 'faltan permisos de lectura en la base para el acceso fiscal',
        'fiscal_database_unavailable': 'no se pudo acceder a la configuración o conexión fiscal local',
        'fiscal_database_profile_invalid': 'el perfil fiscal tiene privilegios administrativos no permitidos',
        'fiscal_python_missing': 'falta openpyxl 3.1.5 o Selenium 4.48.0 en el Python fiscal configurado',
        'fiscal_browser_missing': 'Firefox o geckodriver no están disponibles en este perfil',
        'fiscal_browser_timeout': 'el navegador tardó demasiado en iniciar o cerrar la comprobación previa',
        'fiscal_browser_start_failed': 'Firefox no pudo completar la comprobación previa de funcionamiento',
        'fiscal_python_timeout': 'la comprobación del entorno fiscal tardó demasiado',
        'fiscal_runtime_protocol': 'la comprobación del entorno fiscal devolvió una respuesta inesperada',
    }.get(code, 'falta un componente del procedimiento local')
    return f'{operation} no pudo iniciarse: {detail}. No se consultó ARCA; requiere mantenimiento local.'
