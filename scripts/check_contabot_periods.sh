#!/usr/bin/env bash
# Gate de #168: inventario estático + handlers y regresión, runner canónico.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/check_period_input.py
exec bash scripts/run_tests.sh \
  tests/plugins/platforms/telegram/test_period_input.py \
  tests/gateway/test_portal_iva_flow.py tests/gateway/test_portal_iva_ddjj_153.py \
  tests/gateway/test_portal_iva_ddjj_resultados.py tests/gateway/test_lea_fiscal_query_flow.py \
  tests/gateway/test_agip_ddjj_flow.py tests/gateway/test_comarb_ddjj_flow.py \
  tests/gateway/test_comarb_ddjj_periodos.py tests/gateway/test_comarb_ddjj_controls.py \
  tests/gateway/test_comarb_ddjj_delivery.py tests/gateway/test_comarb_deducciones_flow.py \
  tests/gateway/test_provinciales_iibb_139.py tests/gateway/test_sct_status_sol.py \
  tests/gateway/test_ccma_deterministic_dispatch.py tests/gateway/test_telegram_persistent_menu.py \
  tests/gateway/test_mis_retenciones_157.py tests/gateway/test_telegram_menu_contract.py "$@"
