"""Bounded CCMA diagnostics: never retain raw stdout, exceptions or page text."""
import json
import logging
import os
import re
from datetime import datetime, timezone

STAGES = {'preflight', 'access', 'browser', 'login', 'service', 'subject', 'period', 'table', 'source', 'workbook', 'delivery'}
CODES = set('login_credentials_rejected failure_screenshot_saved failure_screenshot_unavailable login_username_submitted login_password_submitted login_transition_timeout period_range_required period_range_invalid preflight_failed blocked_target login_host_invalid login_state_unexpected login_user_mismatch captcha_attempts_exhausted login_not_verified blocked_redirect human_gate_after_password ccma_not_found blocked_ccma_redirect subject_not_verified subject_selection_control_missing period_query_controls_missing source_result_unchanged canonical_access_invalid canonical_access_changed_or_ambiguous canonical_access_unavailable source_table_schema_unverified source_table_header_ambiguous source_table_ambiguous source_table_evaluation_failed source_frame_ambiguous source_copied runner_error runtime_unavailable source_integrity fiscal_output_too_large captcha_request_invalid captcha_path_invalid captcha_image_invalid captcha_request_repeated captcha_state_invalid fiscal_database_permissions fiscal_database_profile_invalid fiscal_database_unavailable timeout cancelled unexpected_error unknown_runner_status runner_exit_error source_missing workbook_failed ccma_amount_unreadable delivery_failed complete diagnostic_invalid fiscal_python_missing fiscal_browser_missing fiscal_runtime_missing'.split())
HEADINGS = {'detalle', 'periodo', 'impuesto', 'concepto', 'subpcto', 'subcpto', 'descripcion', 'fechamovimiento', 'debe', 'haber', 'saldo', 'subconcepto', 'fecha', 'movimiento', 'subconcept', 'debitos', 'creditos'}
CODES.update({'service_catalog_wait', 'service_entry_ready', 'service_catalog_load_timeout',
              'service_opened', 'service_open_timeout', 'login_verified',
              'ccma_entry_ambiguous', 'ccma_account_load_timeout', 'subject_verified', 'subject_selection_submitted'})
CODES.update({'captcha_answer_received', 'captcha_image_changed', 'captcha_input_verified',
              'captcha_input_not_retained', 'login_fields_verified', 'login_fields_not_retained',
              'captcha_rejected', 'stage_evidence_unavailable'})
KINDS = {'TimeoutError', 'TimeoutException', 'Error', 'TypeError', 'ReferenceError', 'ValueError', 'OSError'}


class Diagnostics:
    def __init__(self, root):
        self.reference = root.name
        self.stage = 'preflight'
        self.fd = os.open(root / 'diagnostic.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def record(self, event):
        if not isinstance(event, dict):
            event = {'code': 'diagnostic_invalid'}
        if isinstance(event.get('stage'), str) and event['stage'] in STAGES:
            self.stage = event['stage']
        row = {'stage': self.stage, 'at': datetime.now(timezone.utc).isoformat()}
        if 'code' in event:
            row['code'] = event['code'] if isinstance(event['code'], str) and event['code'] in CODES else 'unknown_runner_status'
        if isinstance(event.get('error_kind'), str) and event['error_kind'] in KINDS:
            row['error_kind'] = event['error_kind']
        if type(event.get('exit_code')) is int:
            row['exit_code'] = event['exit_code']
        shot = event.get('screenshot')
        if (isinstance(shot, dict) and shot.get('file') == 'failure.png'
                and type(shot.get('bytes')) is int and 0 < shot['bytes'] <= 8000000
                and isinstance(shot.get('sha256'), str) and re.fullmatch('[a-f0-9]{64}', shot['sha256'])):
            row['screenshot'] = {key: shot[key] for key in ('file', 'bytes', 'sha256')}
        if isinstance(event.get('login_state'), dict):
            row['login_state'] = {key: event['login_state'][key] for key in ('error_visible','password_visible','captcha_visible','at_auth') if type(event['login_state'].get(key)) is bool}
        if isinstance(event.get('tables'), list):
            row['tables'] = []
            for table in event['tables'][:30]:
                if not isinstance(table, dict):
                    continue
                clean = {k: min(max(table[k], 0), 100000) for k in ('index', 'rows', 'header_matches') if type(table.get(k)) is int}
                clean['headings'] = [[cell if isinstance(cell, str) and cell in HEADINGS else '?' for cell in cells[:20]]
                                     for cells in (table.get('headings', []) if isinstance(table.get('headings'), list) else [])[:3] if isinstance(cells, list)]
                row['tables'].append(clean)
        data = (json.dumps(row, ensure_ascii=False) + '\n').encode()
        with os.fdopen(os.dup(self.fd), 'ab') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def failure(self, code, exit_code=None, error=None):
        safe = code if isinstance(code, str) and code in CODES else 'unknown_runner_status'
        self.record({'code': safe, 'exit_code': exit_code, 'error_kind': type(error).__name__ if error else None})
        logging.getLogger(__name__).warning('ccma_failure reference=%s stage=%s code=%s', self.reference, self.stage, safe)
        labels = {'preflight': 'controles previos', 'access': 'verificación del acceso', 'browser': 'apertura del navegador', 'login': 'ingreso a ARCA', 'service': 'apertura de CCMA', 'subject': 'selección del contribuyente', 'period': 'consulta del período', 'table': 'lectura de movimientos', 'source': 'validación de la fuente', 'workbook': 'generación del Excel', 'delivery': 'entrega del Excel'}
        reason = {'login_credentials_rejected': 'ARCA respondió «Clave o usuario incorrecto» para el acceso guardado', 'login_not_verified': 'el ingreso no confirmó la transición esperada', 'source_result_unchanged': 'la página sigue mostrando el resultado anterior', 'source_table_schema_unverified': 'no se encontró el encabezado esperado', 'source_table_header_ambiguous': 'hay encabezados repetidos', 'source_table_ambiguous': 'hay más de una tabla candidata', 'source_table_evaluation_failed': 'falló la lectura técnica de la tabla', 'source_frame_ambiguous': 'hay más de un marco de CCMA', 'subject_not_verified': 'no se pudo identificar de forma única el contribuyente', 'period_query_controls_missing': 'no se encontraron los controles del período', 'ccma_not_found': 'no se encontró la entrada a CCMA'}.get(safe, 'el paso no pudo completarse')
        reason = {
            'service_catalog_load_timeout': 'ARCA no terminó de cargar el catálogo de servicios',
            'service_open_timeout': 'el ingreso a ARCA se completó, pero CCMA no terminó de abrir después de seleccionarlo',
            'ccma_account_load_timeout': 'CCMA no terminó de cargar la cuenta y el período con identidad verificable',
            'ccma_entry_ambiguous': 'hay más de una entrada visible a CCMA y no se eligió ninguna',
        }.get(safe, reason)
        result = 'No se entregó ningún libro anterior.' if self.stage in {'workbook', 'delivery'} else 'No se generó ni se reenvió un libro anterior.'
        next_step = 'El acceso debe revisarse mediante la gestión segura de credenciales; no envíes claves por el chat.' if safe == 'login_credentials_rejected' else 'Requiere revisión; no repitas la consulta todavía.'
        return f'CCMA se detuvo en {labels[self.stage]}: {reason}. {result} Referencia: {self.reference} · {safe}. {next_step}'

    def close(self):
        os.close(self.fd)
