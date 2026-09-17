"""ARCA access from the canonical database, confined to local process memory."""
import asyncio
import json
import logging
import re

from plugins.platforms.telegram.fiscal_execution import terminate_owned_group
from plugins.platforms.telegram.fiscal_scope import assert_marked, mark_verified_sql

logger = logging.getLogger(__name__)


def lookup_connection():
    """Same portable profile, without privileged lookup fallback."""
    from contabot_pg import psql_invocation
    return psql_invocation('lookup')


class FiscalDatabaseError(RuntimeError):
    """Safe local diagnostic, never a password, SQL statement or raw stderr."""


def database_error(code):
    logger.error("fiscal_database_failure code=%s", code)
    return FiscalDatabaseError(code)


def access_sql(contributor_id, cuit, slug, holder_cuit):
    if (type(contributor_id) is not int or contributor_id <= 0
            or not re.fullmatch(r'[0-9]{11}', cuit or '')
            or not re.fullmatch(r'[0-9]{11}', holder_cuit or '')
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug or '')):
        raise ValueError('canonical_access_invalid')
    # One snapshot: unique active access and both active parties. Never guess
    # a representative from a name or silently replace the selected identity.
    return f"""WITH candidates AS (
      SELECT btrim(h.cuit) AS usuario, a.usuario AS login_usuario,
             console.fn_acceso_descifrar(a.contrasena) AS password,
             btrim(c.cuit) AS representado, c.slug
      FROM public.tbl_representaciones r
      JOIN public.tbl_entidades e ON e.id_entidad=r.id_entidad AND e.activo AND e.nombre='ARCA'
      JOIN public.tbl_contribuyentes c ON c.id_contribuyente=r.id_contribuyente_representado AND c.activo
      JOIN public.tbl_contribuyentes h ON h.id_contribuyente=r.id_contribuyente_representante AND h.activo
      JOIN public.tbl_accesos a ON a.id_entidad=e.id_entidad AND a.id_contribuyente=h.id_contribuyente AND a.activo
      WHERE r.activo AND c.id_contribuyente={contributor_id}
    ) SELECT json_build_object('usuario',usuario,'password',password,'representado',representado)::text
      FROM candidates WHERE (SELECT count(*) FROM candidates)=1
      AND representado='{cuit}' AND slug='{slug}' AND usuario='{holder_cuit}'
      AND (login_usuario IS NULL OR btrim(login_usuario)=usuario)
      AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user);
    """


async def _query(sql):
    process = None
    try:
        from contabot_pg import psql_invocation
        command, environment = psql_invocation('fiscal')  # mandatory profile; NO sudo fallback
        process = await asyncio.create_subprocess_exec(
            *command, '-qAt', '-v', 'VERBOSITY=sqlstate', '-c', sql, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        raw, error = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode:
            code = ('fiscal_database_permissions' if re.search(rb'\b42501\b', error)
                    else 'fiscal_database_unavailable')
            raise database_error(code)
        if len(raw) > 65536:
            raise database_error('fiscal_database_unavailable')
        return raw
    except FiscalDatabaseError:
        raise
    except (ImportError, OSError, RuntimeError, asyncio.TimeoutError):
        raise database_error('fiscal_database_unavailable') from None
    finally:
        await terminate_owned_group(process)


async def require_fiscal_database():
    # EXPLAIN without ANALYZE checks the actual consumer's columns, not a
    # duplicated permission list. No credential rows are executed or returned.
    sql = access_sql(1, '00000000000', 'preflight-synthetic', '00000000000')
    raw = await _query("""BEGIN READ ONLY;
        SELECT 'fiscal_sql_ready' FROM pg_roles WHERE rolname=current_user
        AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
        EXPLAIN """ + sql + " ROLLBACK;")
    if not raw.splitlines() or raw.splitlines()[0] != b'fiscal_sql_ready':
        raise database_error('fiscal_database_profile_invalid')


async def canonical_access(contributor_id, cuit, slug, holder_cuit):
    raw = await _query(access_sql(contributor_id, cuit, slug, holder_cuit))
    try:
        rows = raw.splitlines()
        if len(rows) != 1:
            raise ValueError('canonical_access_changed_or_ambiguous')
        value = json.loads(rows[0])
        if (set(value) != {'usuario', 'password', 'representado'}
                or value['usuario'] != holder_cuit or value['representado'] != cuit
                or not isinstance(value['password'], str) or not 0 < len(value['password']) <= 8192):
            raise ValueError('canonical_access_invalid')
        return json.dumps({'type': 'access', **value}, separators=(',', ':')).encode() + b'\n'
    except (TypeError, KeyError, json.JSONDecodeError):
        raise ValueError('canonical_access_unavailable') from None


async def verify_representation(telegram_id, item, entity, verified_cuit):
    """Persist only a subject identity already verified by the portal runner."""
    raw = await _query(mark_verified_sql(telegram_id, item, entity, verified_cuit))
    try:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        assert_marked(rows)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('representation_verification_failed') from exc
