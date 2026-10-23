# Hermes de ContaBot: actualización a 0.22.5 — 23/10/2026

Este documento registra la integración y sus pruebas. El cierre operativo (publicación, PID y
conexión de Telegram) se registra en Obsidian, en «Actualización — Hermes ContaBot 0.22.5
2026-10-23», y en `deployment/registro-despliegues.md` de ContaBot. Este informe por sí solo no
prueba que el servicio haya sido reiniciado.

## Alcance y versiones

[HECHO] El propietario pidió revisar y aplicar el plan de actualización del 23/10 (vault, «Plan —
Actualización Hermes ContaBot 0.22.5 2026-10-23»). La actualización conserva la botonera, los
flujos de ContaBot y el menú técnico; no cambia reglas contables, PostgreSQL, los ejecutores
fiscales, credenciales, modelos, skills ni memoria propia.

- Baseline productivo: `96cca81572ba7b63e01869895d79a64a198e3dd7`, Hermes 0.21.2, rama `main`
  del fork, release `hermes-hotfix-96cca81572ba`.
- Release oficial: [v0.22.5](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.5),
  commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (22/10/2026).
- Base común de integración: `939e45c91d751fadd94dcd1b873ac3cb44846213` (v0.21.2 / v2026.9.11).
  Entre la base y v0.22.5: 24.426 commits de upstream; 198 commits propios del fork.
- Rama de integración: `update/contabot-hermes-0.22.5`; commit de merge
  `a2131798c237e6c91df3dc2f0aedc183e3ea790e` con los dos padres (baseline y release oficial).
- Método: el del 12/09 (merge de tres vías en un worktree aparte, sin `hermes update`, sin
  `checkout main` ni `reset --hard`). `hermes update` en un fork sincroniza con
  `git push origin main --force-with-lease` (`hermes_cli/update_cmd_git.py`); no se usa.

## Desvío respecto del plan: Python 3.14

[HECHO] El plan preveía conservar Python 3.11.15. En 0.22.5 **las 46 dependencias core de
`pyproject.toml` llevan el marcador `python_version >= '3.14'`** y `[tool.uv] environments` fija
`>= '3.14'`; `requires-python` sigue diciendo `>=3.11,<3.15`, pero una instalación en 3.11 queda sin
`ruamel.yaml`, `rich`, `psutil`, `requests`, `pydantic` y el resto (comprobado: `hermes doctor`
fallaba al importar `ruamel`). El instalador oficial elige 3.14 por defecto (`scripts/install.sh`).
Decisión: entorno nuevo con **CPython 3.14.7 gestionado por uv** (`uv python install 3.14`),
instalado con `uv sync --frozen` desde el `uv.lock` regenerado, extras `messaging`, `contabot`,
`voice` y `bedrock`. El Python 3.11.15 anterior se conserva en `venv-0.21.2`.

[HECHO] Los scripts de ContaBot que corren con este entorno (Portal IVA por lotes, AGIP, COMARB,
`lib/`) compilan con 3.14 y sus dependencias (`selenium==4.48.0`, `openpyxl==3.1.5`,
`PyMuPDF==1.28.2`) tienen ruedas cp314; `uv pip check` conforme salvo el aviso esperado de upstream
(`browser-harness` pide Pillow 12.3 y el override de upstream instala 13.0.0).

## Adaptación concreta

Cinco archivos en conflicto; se conservó la implementación oficial nueva y se reconectaron las
piezas del fork sobre ella:

- `plugins/platforms/telegram/adapter.py` (seis hunks): imports (`atomic_replace` + nuevo
  `TelegramTopicRoutingMixin`); teclado persistente en `_rich_payload_base`; menú técnico
  `_register_technical_command_menu` sobre los helpers nuevos (`_bot_commands`,
  `hermes_cli.commands_platforms`, descubrimiento de skills fuera del loop con `asyncio.to_thread`
  como hace upstream); `add_error_handler` y `_handle_application_error` junto al bloque nuevo de
  ids de handlers core y `_wire_plugin_handlers`; `_record_bot_rejection` con la firma nueva de
  `_handle_callback_query`; el foro sigue **borrando** el menú de comandos (política ContaBot: menú
  sólo en el DM del usuario técnico), donde upstream ahora lo publica.
- `pyproject.toml`: bloque de extras de upstream más el extra `contabot`; `uv.lock` regenerado
  (`uv lock`, 148 líneas añadidas sobre el de upstream).
- `tests/gateway/test_gateway_platform_event_hook.py`: upstream eliminó el bloque; se conservan
  las pruebas del fork con el conteo nuevo (ocho handlers core).
- `tests/gateway/test_telegram_forum_commands.py`: la prueba nueva de upstream del escaneo de
  skills fuera del loop se adapta al menú técnico; el foro no publica menú.
- Fusionados sin conflicto y verificados: `gateway/authz_mixin.py` (`authorize_principal`
  dinámico), `gateway/platforms/base.py` (`SendResult.delivered_filename`), `scripts/run_tests.sh`
  y siete tests de Telegram del fork.
- Los 46 módulos propios de `plugins/platforms/telegram/` no se reescribieron. Los 77 nombres que
  importan de Hermes existen en el árbol fusionado (comprobación por importación real).
- Ajustes de pruebas del fork al contrato nuevo: `hermes_cli.commands` →
  `hermes_cli.commands_platforms` (menú técnico), `@pytest.mark.linux_only` →
  `@pytest.mark.platforms("linux")` (713), y el PDF de referencia
  `docs/hermes-kanban-v1-spec.pdf`, que upstream retiró, reemplazado por otro PDF versionado.
- `git diff --check` sólo avisa líneas en blanco finales en archivos de upstream; `compileall` del
  árbol con 3.14: correcto.

## Runtime y pruebas

Python 3.14.7, 135 paquetes; versiones exactas en `runtime-0.22.5.txt`. Entorno de pruebas
independiente (`venv-0.22.5` del worktree) y entorno de producción relocalizable
(`~/.hermes/hermes-agent/venv-0.22.5`, sincronizado desde el checkout principal antes de activar).

[HECHO] Migraciones de perfil probadas en una copia de `config.yaml` (sin secretos) con
`hermes doctor` en bubblewrap sin red: `_config_version` 38 → 51, ningún otro cambio; sin claves
deprecadas; estructura de directorios creada. El perfil real no se tocó.

[HECHO] Copia consistente de `state.db` (API de backup de SQLite) abierta con el runtime nuevo y de
vuelta con el anterior: `quick_check=ok`, **70 sesiones y 34.714 mensajes** en los dos casos;
`journal_mode=wal`. No se probó migración sobre la base activa.

[HECHO] Suite enfocada (314 archivos: Telegram, flujos ContaBot, `tests/hermes_state`,
contención de estado) con el runner oficial por archivo, en bubblewrap con `HERMES_HOME` temporal,
filesystem real de sólo lectura y sin red. La primera tanda fracasó por el sandbox, no por el
código, y se repitió por archivo hasta aislar cada causa:
- `node` ausente del PATH del sandbox: todos los fallos de `test_fiscal_*`, `test_ccma_*` y
  `test_documentos_contabot` (la suite exige el runtime Node offline). Con
  `~/.hermes/node/bin` en el PATH: **verdes**.
- `HOME` neutral del sandbox: `test_agip_ddjj_flow::test_worker_starts_in_new_session` compara
  una ruta bajo `/home/pancho`; verde con `HOME` real.
- Directorio git del worktree bajo `~/.hermes`: el guardián de upstream («file I/O against the
  REAL hermes home») abortaba pruebas de `tests/hermes_state` y de Telegram que escriben el
  marcador `hermes-update-pull` en el gitdir. Desde un **clon independiente** en `/tmp` los 15
  archivos de `hermes_state` afectados pasan completos (exit 0).
- Cinco archivos ajustados (713, menú técnico, foro, event hook, PDF): **55 pruebas, 0 fallos**.

[HECHO] Fallo preexistente, no introducido por el merge:
`tests/gateway/test_fiscal_adapter_integration.py::test_real_menu_to_canonical_identity_period_and_dispatch[ccma|sct]`
(2 pruebas) falla igual sobre el árbol anterior (`git archive` de 96cca81572 con el venv 0.21.2):
la prueba invoca `_handle_text_message` con un contexto sin `bot` y el puente de CAPTCHA del fork
usa `context.bot`. Se deja como está y se declara.

[DESCONOCIDO al escribir esto] La regresión amplia (`scripts/run_tests.sh` completa, ~50.000
pruebas de upstream) corre desde el clon independiente; su resultado se anexa en una sección
final antes de activar.

## Respaldo y recuperación

Respaldo privado en `/home/pancho/.local/state/contabot/hermes-update-20261023-HzBI8j` (0700):

- `hermes-agent-before.tar` (código, `.git` y `venv` 3.11): SHA-256
  `b4bdaa459ed7ad6f14bc3efdbf5ebd01cfae870c6d103ab8acf161e6de3ffb71`.
- `profile-before.tar` (`~/.hermes` sin el repositorio: configuración, identidad, skills, memoria,
  historial): SHA-256 `65e4d399cad31353f63eff79a95db6a118520e54fdbaf5849586dea7d7c5fc7c`. Tomado
  con el gateway activo (`tar` avisó «file changed»); el `state.db` consistente se respalda en la
  activación con el gateway detenido.
- `systemd-user-before.tar` (unidad y drop-ins): SHA-256
  `ea746865775d54085c2a88b16f71774a80e41d08960b12b9b71878273329a027`.
- Los archivos de respaldo son 0600 y no se publican en Git.
- Ante fallo de arranque: `systemctl --user stop`, restaurar el drop-in anterior
  (`WorkingDirectory=…/hermes-hotfix-96cca81572ba`), `venv-0.21.2` de vuelta como `venv`,
  `daemon-reload`, `start`. No restaurar `state.db` automáticamente: preservar primero la
  actividad nueva y evaluar su compatibilidad (ya comprobada en los dos sentidos).

## Activación (procedimiento; requiere root y ventana sin corridas)

El activador canónico es `deployment/deploy_hermes_hotfix.py` de ContaBot (release propiedad de
root en `~/.local/share/contabot/releases/hermes-hotfix-<sha12>`, drop-in
`zz-115-documentos-release.conf`, `daemon-reload`, `restart`, comprobación del `cwd` del gateway).
Para 0.22.5 hacen falta tres pasos que el activador no hace: cambiar el venv, sellar la versión y
reiniciar el worker de Portal IVA por lotes, que usa el mismo venv.

1. Verificar: `main` del fork en el commit de merge y publicado; sin trabajos fiscales en curso
   (misma consulta SQL del activador); `~/.hermes/hermes-agent/venv-0.22.5` sincronizado desde el
   checkout principal (`__editable__` apunta a `/home/pancho/.hermes/hermes-agent`).
2. `systemctl stop contabot-portal-iva-lote-worker.service` y
   `systemctl --user stop hermes-gateway.service` (como `pancho`).
3. Respaldo consistente de `state.db` con la API de backup de SQLite a la carpeta de evidencia
   (0600, SHA-256).
4. `mv venv venv-0.21.2 && mv venv-0.22.5 venv` en `~/.hermes/hermes-agent`; mover
   `hermes_agent.egg-info` (declara 0.21.2) a la evidencia; comprobar
   `venv/bin/python -c "import hermes_cli, telegram, selenium, openpyxl, pymupdf, boto3"`.
5. Crear la release como root: `mkdir -m 755 …/hermes-hotfix-a2131798c237`,
   `git archive HEAD | tar -x`, `chmod 755` de directorios, y el sello
   `install-stamp.json` generado con `scripts/write_install_stamp.py --commit a2131798… --branch
   main --base-version 0.22.5 --distance 199 --source local --update-mechanism external`
   (sin sello, una copia sin `.git` informa `vunknown`; con sello, `v0.22.5+199`).
6. `python3 deploy_hermes_hotfix.py --repo ~/.hermes/hermes-agent --evidence <dir> --reuse-release`
   (coteja cada archivo de la release con `git archive HEAD`, tolera el sello adicional, cambia el
   drop-in y reinicia el gateway; si falla, repone el drop-in anterior y reinicia).
7. `systemctl start contabot-portal-iva-lote-worker.service`.
8. Comprobar: `active/running`, PID nuevo con `cwd` en la release, `gateway_state.json`
   `code_version=0.22.5+199`, Telegram `connected`, journal sin ERROR/CRITICAL/Traceback,
   `state.db` con los mismos conteos, `hermes version` sin el aviso de «update sin reinicio».

## Próximas actualizaciones

El fork sigue siendo el fork. Para actualizarlo: fijar la release oficial, respaldar, integrar
contra su base en una copia, repetir las pruebas de ContaBot y sesiones (con `node` en el PATH y
desde un clon fuera de `~/.hermes`), publicar y activar con el sello de versión. Upstream ya sólo
resuelve dependencias para Python 3.14: no volver a 3.11.

[DESCONOCIDO] No se afirmará una conversión bancaria o consulta fiscal real a partir de mocks,
imports o del solo hecho de que Telegram conecte; la corrida real por Telegram es del propietario.
