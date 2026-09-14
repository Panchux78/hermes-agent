"""Bounded, local preflight in the fiscal Python and the selected Node probe."""
import asyncio
import os
from pathlib import Path

from plugins.platforms.telegram.fiscal_execution import terminate_owned_group
from plugins.platforms.telegram.fiscal_credentials import require_fiscal_database

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


async def _check(command, environment, expected):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command, env=environment, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        stdout, _ = await asyncio.wait_for(process.communicate(), 25)
        return process.returncode == 0 and stdout.strip() == expected
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        await terminate_owned_group(process)


async def require_fiscal_runtime(python, node, probe, home, *, canonical=True):
    if (not python or not Path(python).is_absolute() or not Path(python).is_file()
            or not os.access(python, os.X_OK) or not node or not probe.is_file()):
        raise RuntimeError('fiscal_runtime_missing')
    if canonical:
        await require_fiscal_database()
    environment = browser_environment(home, python=python)
    if not await _check([str(python), '-I', '-B', '-c', PYTHON_CHECK], environment, b'fiscal_python_ready'):
        raise RuntimeError('fiscal_python_missing')
    if not await _check([node, str(probe), '--preflight'], environment, b'result=fiscal_runtime_ready'):
        raise RuntimeError('fiscal_browser_missing')


def unavailable_message(operation, code):
    detail = {
        'fiscal_database_permissions': 'faltan permisos de lectura en la base para el acceso fiscal',
        'fiscal_database_unavailable': 'no se pudo acceder a la configuración o conexión fiscal local',
        'fiscal_database_profile_invalid': 'el perfil fiscal tiene privilegios administrativos no permitidos',
        'fiscal_python_missing': 'falta openpyxl 3.1.5 o Selenium 4.48.0 en el Python fiscal configurado',
        'fiscal_browser_missing': 'Firefox o geckodriver no están disponibles en este perfil',
    }.get(code, 'falta un componente del procedimiento local')
    return f'{operation} no pudo iniciarse: {detail}. No se consultó ARCA; requiere mantenimiento local.'
