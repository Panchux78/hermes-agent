# Hermes de ContaBot: actualización a 0.23.2 — 18/11/2026 (Ágora #184, paso 1)

Integración, pruebas, activación y prueba en producción por Telegram. El cierre operativo se
registra además en `deployment/registro-despliegues.md` de ContaBot y en la nota de Obsidian.

## Versión elegida y distancia

- [HECHO] Última versión oficial estable: [v0.23.2](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.2)
  (`ae2a81ccc0`, 16/11/2026), marcada «Latest». Las posteriores (`v0.23.2+canary.*`) son
  prerelease nocturnas: no se usan para producción.
- [HECHO] Baseline productivo: `c330cb6b91` (fork, v0.22.5 + 247 commits propios), release
  `hermes-hotfix-c330cb6b9132`.
- [HECHO] Base común `bf0203a794` (v0.22.5). Upstream v0.22.5 → v0.23.2: 11.178 archivos,
  +707.727/−195.076 líneas. Fork sobre la base: 151 archivos. Tocados por ambos: 9
  (`plugins/platforms/telegram/adapter.py`, `gateway/platforms/base.py`, `gateway/authz_mixin.py`,
  `pyproject.toml`, `uv.lock`, `scripts/run_tests.sh` y tres pruebas de Telegram).
- Método: el de 0.21.2 y 0.22.5 (merge de tres vías en un worktree aparte, sin `hermes update`,
  que en un fork empuja con `--force-with-lease`).

## Integración

Merge `e942de1c3a` (padres `c330cb6b91` y `ae2a81ccc0`). Cuatro archivos en conflicto:

- `plugins/platforms/telegram/adapter.py` (cinco bloques):
  - imports: `atomic_replace` del fork + `TelegramVaultCodeMixin` nuevo;
  - handlers: los de upstream (rich message, dados, contacto, video note) envueltos en
    `execution_origin.incoming` del fork (procedencia por update, #162);
  - texto: primero el código de bóveda de upstream (nunca un turno), después la cadena ContaBot;
  - media: los flujos de documentos ContaBot antes de `admit_media` de upstream;
  - `_send_local_file`: upstream lo movió al mixin `telegram_media_flood.py`; se conserva el de
    upstream y se le agrega `SendResult.delivered_filename` del fork.
- `gateway/authz_mixin.py`: `authorize_principal` dinámico del fork + `chat_type` normalizado.
- `tests/gateway/test_telegram_thread_fallback.py`: pruebas nuevas de upstream.
- `uv.lock`: el de v0.23.2 regenerado con uv 0.13.5; sólo suma el extra `contabot`.

Ajustes posteriores, cada uno con su prueba:

- `ad80610de8`: upstream quitó `from pathlib import Path as _Path` del adaptador y el fork lo usa
  (menú y worker de Portal IVA). Era la causa de 76 fallos.
- `d6b34bdbc6`: el menú y el puente de CAPTCHA usan `getattr(context, 'bot', None) or self._bot`
  (upstream llama a los handlers con `context=None`); prueba de video notes buscando el handler por
  `__wrapped__`; prueba propia de handlers con los nueve núcleo de 0.23.2 (dados y contacto).
- `e464cd66d8`: la cadena ContaBot (puente de CAPTCHA y flujos) corre sólo con chat y remitente
  (siempre en Telegram; las pruebas de upstream mandan mensajes mínimos), el botón Menú queda afuera;
  `period_input` importa `TelegramError` al usarlo; `sifere_actividades_flow.text` tolera mensajes sin
  chat; la prueba propia del bloqueo de aclaraciones sigue el contrato nuevo (0.23.2 corta la espera
  con texto libre y lo rutea al runner: `tests/gateway/test_clarify_active_session_bypass.py`).
- Los 45 módulos propios importan en el árbol fusionado; `compileall` correcto.

## Runtime, configuración y estado

- [HECHO] Python 3.14.7 (igual que 0.22.5). Venv relocalizable con `uv sync --frozen`, extras
  `messaging`, `contabot`, `voice` y `bedrock`: 113 paquetes (`runtime-0.23.2.txt`), idéntico al de
  pruebas. Cambian 46 versiones (openai 2.24.0 → 2.54.0, uvicorn 0.41.0 → 0.54.0, starlette 1.3.1 →
  1.9.0, boto3 1.42 → 1.43, numpy 2.4.3 → 2.5.4, entre otras); python-telegram-bot, openpyxl,
  selenium y PyMuPDF sin cambios. Salen `pytz`, `setuptools` y `tenacity`: ni el fork ni ContaBot los
  importan.
- [HECHO] Configuración: `_config_version` 51 → 53. Probado antes en una copia, en bubblewrap sin
  red: ningún otro cambio. El gateway hizo la misma migración al arrancar (verificado con `diff`).
- [HECHO] `state.db`: copia consistente abierta con el código nuevo y con el anterior, en los dos
  sentidos: `quick_check=ok`, 70 sesiones y 34.985 mensajes; después de activar, los mismos conteos.

## Pruebas (brief v60: enfocadas + producción, autorizado por Pancho el 18/11 09:40)

- [HECHO] Suite enfocada con el runner oficial, desde un clon fuera de `~/.hermes` y `node` en el
  PATH: 177 rutas (las 65 pruebas propias del fork, todas las de Telegram de upstream y
  `tests/hermes_state`). Primera corrida: 86 archivos con fallos; después de los ajustes, quedan
  cinco pruebas que fallan igual en el árbol de producción anterior:
  - `test_fiscal_adapter_integration` ccma/sct: anterior a la confirmación de período de la #168;
  - `test_restricted_lookup_uses_portable_profile_and_never_falls_back`: anterior a la procedencia
    de la #162 (el entorno suma `CONTABOT_ORIGEN_*`);
  - `test_documentos_contabot::test_lote_historial_usa_ruta_devuelta_por_documentos`.
- [HECHO] Los archivos de Telegram de upstream que fallaban pasan en v0.23.2 pura y pasan después
  de los ajustes.
- [HECHO] La suite completa (~59.480 pruebas) no se corrió entera: en este servidor llevaría más de
  40 horas con cinco procesos, en la máquina de producción. Pancho aceptó enfocadas + producción.

## Activación (18/11/2026, 09:39–09:41 ART)

- Respaldos en `~/trabajo/184/activacion/` (0700): `config-before.yaml`
  (`86830754…`), `state-before.db` (API de backup de SQLite, `b687e046…`),
  `systemd-user-before.tar` (`306a9597…`); `hermes_agent.egg-info` del checkout retirado ahí.
- `main` del fork avanzado a `e464cd66d8` (ff). Release `hermes-hotfix-e464cd66d8f2` creada como
  root con `git archive` y sello `install-stamp.json` (`0.23.2+251`, `updateMechanism: external`).
- Venv: `venv` → `venv-0.22.5`, `venv-0.23.2` → `venv`.
- `deploy_hermes_hotfix.py --reuse-release --contabot-release ficha-fiscal-db8494e838ec-a2`:
  `ACTIVATED`; el activador comprobó colas libres y sin pruebas abiertas antes de reiniciar.
- Resultado: gateway en la release nueva, CPython 3.14.7, `code_version=0.23.2`, Telegram
  `connected` (09:41:34, intento 1/8), journal sin Traceback/CRITICAL; worker de Portal IVA activo
  con el venv nuevo. `hermes --version`: `v0.23.2+251.ge464cd6`.
- Aviso nuevo, una vez al arrancar: «install out of sync (npm/pnpm not installed or outdated)». Es
  de las herramientas Node de la interfaz web de Hermes, que ContaBot no usa; no afecta a Telegram.

Vuelta atrás: detener gateway y worker, `venv` ↔ `venv-0.22.5`, repetir el activador con la
release `hermes-hotfix-c330cb6b9132` (sigue en disco). `state.db` y la configuración son compatibles
con la versión anterior (comprobado).

## Prueba en producción por Telegram (18/11/2026, 10:00–13:40 ART)

Método: `~/trabajo/184/prueba/menu184.py` (autorizado por Pancho, 09:40) construye el
`TelegramAdapter` de la release que corre el gateway, con `load_gateway_config()` y la misma
decisión de autorización (`authorize_principal`, tabla de usuarios de ContaBot), y le entrega
toques, textos y documentos por sus handlers. Las respuestas salen al chat real de Pancho. Sin
`update_id` la procedencia queda «prueba» (#162). Contribuyentes de prueba 101 y 188. El resultado
de cada corrida se lee en Operaciones (`tbl_corridas_bot`).

- [HECHO] Menú: recorrido completo de Organismos (siete organismos, Consultar/Preparar/
  Presentar), Bancos, Herramientas, Ayuda y Administración: 152 eventos, sin error. Las 26
  funciones abren y piden su primer dato.
- [HECHO] Funciones de punta a punta:
  - ARCA › Vencimientos: Excel entregado (24 registros).
  - ARCA › CSV de períodos presentados (101, 09/2026): Ventas 6.570 filas y Compras 91 entregados;
    la corrida queda «incompleta» por el renglón de importaciones (patrón ya visto en la #159).
  - ARCA › DDJJ de IVA a Excel (101, 09/2026): Excel entregado, corrida completada.
  - ARCA › Constancias (188): consulta completada.
  - ARCA › SCT (188, 10/2026): XLSX entregado.
  - ARCA › Mis Retenciones (101, IVA, 10/2026): Excel y original, 30 registros iguales a ARCA.
  - ARCA › Mis Comprobantes (101, emitidos, 10/2026): 7.049 comprobantes, iguales a ARCA.
  - ARCA › Preparar período nuevo (101, 10/2026): libros Ventas y Compras entregados.
  - ARBA, Córdoba y Tucumán › Retenciones y Percepciones (188, 10/2026): completadas.
  - Santa Fe (188): incompleta: ARCA no tiene habilitado «API-Santa Fe-COPRIB» para esa clave.
  - AGIP › Retenciones y Percepciones (188): incompleta: el sitio no respondió como se esperaba.
  - COMARB › Deducciones (101, 10/2026): incompleta (falta la clave de ARBA), 7 archivos.
  - COMARB › DDJJ presentadas (188, 09/2026) y Actividades para SIFERE (188, 09/2026): completadas.
  - Bancos › Resumen bancario → Excel, Lote de resúmenes y Cruce con Libros IVA: entregados
    (152 movimientos; cruce con 12 deudores y 6 proveedores).
  - Herramientas › Proteger PDF y Desbloquear PDF: entregados.
  - Administración › BCRA: candidato preparado (73 entidades), no confirmado (no se cambió el
    catálogo).
- [HECHO] Agente: `hermes chat` con el perfil de producción respondió con la fecha correcta y el
  modelo `gpt-6.1-sol` (sin cambios).
- Fallas y su causa, ninguna atribuible a la actualización:
  - CCMA: 188 `subject_not_verified`; 101 `ccma_account_load_timeout`, el mismo hallazgo de la
    #159 (11/11, `deployment/159-pruebas-cc1543.md`).
  - AGIP › DDJJ (188): «no se pudo seleccionar el contribuyente representado»; AGIP DDJJ falla
    seguido también antes (corridas automáticas de hoy 09:02 y 09:06, con 0.22.5).
  - Administración › Mapa de impuestos: «ARCA no devolvió impuestos»; el mismo script falla igual
    con el venv anterior.
  - ARCA › Lote de Libros IVA: el ejecutor bajó todo de ARCA (manifiesto `completado`), pero el
    registro del lote exige `canal='telegram'` (migraciones 0051, 0052, 0061) y una prueba técnica
    queda con canal «prueba»; por Telegram real anduvo esta madrugada (corrida 52872, 0.22.5, código
    sin cambios).
- Límites del método, declarados:
  - SCT (101) y el PDF de la constancia pidieron CAPTCHA; en esos flujos la respuesta queda en un
    `Future` del proceso que los lanzó y la contestación de Pancho llega al gateway real. Hace falta
    la prueba de Pancho en su chat.
  - Ayuda › Hacer una consulta y la conversación de Pancho pasan por el `GatewayRunner`, que el
    arnés no tiene: se probó el agente por CLI; falta un mensaje real de Pancho.
  - El puente de CAPTCHA del panel y los avisos automáticos de vencimientos no se dispararon en la
    ventana de prueba.

## Observaciones para otras conversaciones (no se cambiaron)

- Un flujo que quedó esperando un botón (por ejemplo, Vencimientos en «Excel o ICS») toma el
  próximo texto aunque el usuario haya ido a otra función; «☰ Menú» sólo cancela Actividades y
  DDJJ presentadas. Igual en el código anterior.
- El registro de Lote de Libros IVA por Telegram no admite el canal «prueba» de la #162.
