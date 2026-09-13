# Hermes de ContaBot: actualización a 0.21.2 — 12/09/2026

Este documento registra la integración y sus pruebas. El cierre operativo
(publicación, PID y conexión) se registra en Obsidian, en
«Actualización — Hermes ContaBot 0.21.2 2026-09-12». Este informe local por sí
solo no prueba que el servicio haya sido reiniciado.

## Alcance y versiones

[HECHO] El propietario pidió actualizar Hermes Agent. La actualización conserva
la botonera y los seis flujos existentes de ContaBot; no cambia reglas contables,
PostgreSQL, los ejecutores fiscales, credenciales, modelos, skills ni memoria propia.

- Baseline productivo: `fab4f5095f53cd3bd94130addc662750e6629484`, Hermes 0.20.5,
  rama `feat/telegram-pdf-security`.
- Release oficial: [v2026.9.11](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.11),
  Hermes 0.21.2, commit `939e45c91d751fadd94dcd1b873ac3cb44846213`.
- Base común de integración: release v2026.8.19,
  `fcbd1076a93841fa88855acce810e342a5b78101`.
- Rama de integración: `update/contabot-hermes-0.21.2`.
- [HECHO] Al consultar origin/main durante esta actualización, apuntaba a
  `7552e0f3c0`, versión 0.14.0. No se cambió producción a esa rama ni se
  sobrescribió main. El mensaje antiguo «1 commit atrás» no medía versiones:
  no era una instrucción segura para este checkout superficial.

## Adaptación concreta

Se realizó integración de tres vías en un clon independiente, no sobre el gateway activo.
Se conservó la implementación oficial nueva, resolviendo cinco archivos en conflicto.

- `SendResult.delivered_filename` sigue disponible para validar los nombres remotos.
- La entrega de documentos usa el helper oficial `_send_local_file`; devuelve
  también el filename de la respuesta Telegram. No duplica todo el transporte viejo.
- La botonera, autorización, callbacks, CAPTCHA y manejo de documentos de ContaBot
  se conectan a los handlers nuevos. El teclado persistente también se conserva
  en la nueva ruta de envío con reintentos y en mensajes enriquecidos.
- Los flujos contables se importan al construir el adapter, no al importar
  un adapter opcional sin python-telegram-bot.
- El arreglo anterior del bloqueo de clarify ya está cubierto por el código
  oficial nuevo en `gateway/run_inbound.py`. Se conservó su test de regresión;
  no se volvió a insertar la implementación vieja en `gateway/run.py`.
- Los seis módulos de flujos y `menu_buttons.py` conservan su contenido productivo.
- Se adaptaron las expectativas/importaciones y fixtures parciales de tests
  al contrato vigente: teclado persistente y flujos sin conversación activa.
  No se cambiaron las etiquetas ni el espaciado aprobado de la botonera.

## Runtime y pruebas

Python 3.11.15. Entorno independiente para las pruebas, con las dependencias
de la release y los extras ya instalados de voz y Bedrock. Se conservaron los
paquetes antes presentes. Versiones exactas en `runtime-0.21.2.txt`.
La instalación reproducible usa `uv --no-config` con ese inventario: el override
amplio de cryptography de upstream resolvía 50.0.1 aunque el metadato de esta
release exige 50.0.0. El entorno comprobado satisface el pin declarado.

[HECHO] El Python real usado por el worker AGIP tiene Selenium, OpenPyXL y PyMuPDF.
Portal IVA y los routers siguen usando sus comandos y entornos externos previos.
Esto verifica disponibilidad, no ejecuta portales fiscales.

[HECHO] Una copia consistente de state.db abrió con el SessionDB nuevo y también
con el anterior para verificar retorno: quick_check=ok; 63 sesiones y 33.557
mensajes preservados. No se probó migración sobre la base activa.

La primera tanda amplia se interrumpió: tests oficiales escribieron los caches
`gateway_state.json` y `channel_directory.json` fuera del HERMES_HOME temporal.
No se considera aprobada. Se reforzó con bubblewrap: filesystem real read-only,
directorio de trabajo escribible, ~/.hermes y /tmp aislados y red deshabilitada.
Los caches afectados se preservan en el respaldo privado y se regeneran al
arrancar el gateway; no son la base de sesiones ni documentos de clientes.

[HECHO] Regresión amplia: 849 archivos, 8.434 pruebas aprobadas, 27 fallos,
37 omisiones. Los 27 fallos quedaron resueltos/verificados por archivo:
fixtures del SDK/adapter incompletos y acceso DNS/directorio de locks del
sandbox. No se ocultaron con skips ni se cambiaron las aserciones de seguridad.
Los nueve archivos afectados pasaron: 14 tests de descarga y 139 de los otros
ocho archivos. Para DNS se permitió red conservando el filesystem privado;
se aisló también ~/.local/state/hermes. No hubo envíos reales en esas pruebas.

[HECHO] Corrida final independiente, con las versiones definitivas:
99 archivos de Telegram, flujos ContaBot y estado; 985 aprobadas, cero fallos,
dos omisiones de plataforma. Incluye menús, autorización, callbacks, entrega,
CAPTCHA, cancelación y estado SQLite. Duración 55,3 s.
El resultado amplio y sus rechecks no se presentan como una única corrida
completa en verde. py_compile, git diff --check y uv pip check: correctos.

## Respaldo y recuperación

Respaldo privado en
`/home/pancho/.local/state/contabot/hermes-update-20260912-3hvWwi`, directorio 0700.

- `hermes-agent-before.tar`: código, Git, entornos y backups previos.
  SHA-256 `0641517f80b290b0cc8070ea635407b5c2ee8ed94241c1bf749876576bcda928`.
- `profile-before.tar`: configuración, identidad, skills, memoria, unidad y wrapper.
  SHA-256 `bb3822e88e57a6a7aa238a0ad17d0873df5ba079e879751b6baa2033f1f64925`.
- Los archivos privados de respaldo son 0600 y no se publican en Git.
- Antes de activar se detiene el gateway y se toma otro respaldo consistente
  de state.db. El entorno anterior se conserva sin borrarlo.
- Ante fallo de arranque, detener la versión nueva, restaurar código/venv
  anteriores y arrancar otra vez. No restaurar state.db automáticamente:
  preservar primero cualquier actividad nueva y evaluar su compatibilidad.

## Próximas actualizaciones

Esta integración no convierte el fork en una instalación oficial sin cambios.
Para actualizarlo: fijar la release oficial, respaldar, integrar contra su base
en una copia, repetir las pruebas de ContaBot y sesiones, publicar y activar.
No usar `checkout main`, `reset --hard` ni reinstalar sobre los cambios locales
como atajo. Separar los flujos del motor sería otro trabajo; no se hizo aquí.

La comprobación posterior al arranque verificará PID, versión y conexión Telegram.
[DESCONOCIDO] No se afirmará una conversión bancaria o consulta fiscal real
a partir de mocks, imports o del solo hecho de que Telegram conecte.
