"""ARCA access from the canonical database, confined to local process memory."""
import asyncio
import json
import re

from plugins.platforms.telegram.fiscal_execution import terminate_owned_group


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
             a.contrasena AS password, btrim(c.cuit) AS representado, c.slug
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


async def canonical_access(contributor_id, cuit, slug, holder_cuit):
    sql = access_sql(contributor_id, cuit, slug, holder_cuit)
    process = None
    try:
        from contabot_pg import psql_invocation
        command, environment = psql_invocation('fiscal')  # mandatory profile; NO sudo fallback
        process = await asyncio.create_subprocess_exec(
            *command, '-At', '-c', sql, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode or len(raw) > 65536:
            raise ValueError('canonical_access_unavailable')
        rows = raw.splitlines()
        if len(rows) != 1:
            raise ValueError('canonical_access_changed_or_ambiguous')
        value = json.loads(rows[0])
        if (set(value) != {'usuario', 'password', 'representado'}
                or value['usuario'] != holder_cuit or value['representado'] != cuit
                or not isinstance(value['password'], str) or not 0 < len(value['password']) <= 8192):
            raise ValueError('canonical_access_invalid')
        return json.dumps({'type': 'access', **value}, separators=(',', ':')).encode() + b'\n'
    except (ImportError, OSError, RuntimeError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('canonical_access_unavailable') from None
    finally:
        await terminate_owned_group(process)
