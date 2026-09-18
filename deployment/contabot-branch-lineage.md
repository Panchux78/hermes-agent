# Disposición de líneas históricas de ContaBot/Lea en Hermes

Este documento cierra la condición documental de Ágora #118. El commit de
integración `3850a2ec826d97bc81a84daa3318500ae8eeac31` conserva el árbol de
`b3fef42f35`; los merges `ours` sólo registran ascendencia. La tabla explica,
con evidencia del árbol activo, por qué el contenido de cada línea no se debe
reaplicar literalmente.

| Línea histórica | Clase | Sustitución o decisión verificable |
|---|---|---|
| `feat/agora-99-fiscal-secrets` | Reemplazada / preservada | No tiene líneas ausentes. `plugins/platforms/telegram/fiscal_credentials.py` conserva el acceso cifrado incorporado por `ff32771d58`. |
| `fix/lea-ccma-sct-713` | Reemplazada | El árbol activo contiene la consulta determinista en `ccma_dispatch.py`, el runtime común en `fiscal_runtime.py`, el selector en `fiscal_query_flow.py` y la validación de alcance en `fiscal_scope.py`. El helper eliminado `document_delivery.py` quedó innecesario: `adapter.py` completa `SendResult.delivered_filename`, y `pdf_xlsx_flow.py`/`portal_iva_flow.py` leen ese campo directamente. |
| `review/agora104-sol-725` | Reemplazada | `ccma_diagnostics.py` conserva diagnósticos saneados, captura, estado de login y ubicación tipada del error; `fiscal_interaction.py` conserva la interacción de CAPTCHA. La evidencia es el contenido del árbol activo, no la ascendencia de los commits de revisión. |
| `lea/agora-104-706` | Reemplazada | `ccma_workbook.parse_amount` conserva los formatos decimales observados y `ccma_artifact.publish_named` conserva el versionado sin sobrescritura. El helper de entrega obsoleto tiene el reemplazo indicado arriba. |
| `lea/agora-104-port-base-3d267c0-706` | Pendiente: insumo obligatorio del paso 8 | El árbol activo todavía no contiene toda la portabilidad: faltan `CONTABOT_CLIENTES_ROOT`, conexión mediante `psql_invocation` en AGIP/Portal IVA y la eliminación de rutas absolutas de esta máquina. No se declara promovida. |
| `feat/agora-104-lea-menu-021` | Reemplazada | `operational_menu.py` fue absorbido por `adapter.py` y `menu_buttons.py`; el árbol activo contiene menú por dominio, botones comunes y los ajustes medidos de alineación. `contabot_deployment.py` conserva una parte de las rutas configurables. |
| `feat/agora-104-lea-portable` | Pendiente: insumo obligatorio del paso 8 | Aporta `runtime_python` por perfil, raíces derivadas de `HOME`, `CONTABOT_CLIENTES_ROOT` y `CONTABOT_DB_PROFILE`. Esas piezas deben incorporarse y probarse en el árbol activo antes de declarar instalable una notebook. |
| `feat/db-adapter-006-kanban-store` | Fuera del producto | Es un experimento genérico de almacenamiento Kanban de Hermes; no es una función de ContaBot ni Lea y no forma parte del inventario funcional acordado para #118. |
| `pancho/kanban-postgres-after-update-20260527-204042` | Fuera del producto | Es la continuación del experimento Kanban anterior y además contiene un fix de TTS ajeno. No se instala como componente de ContaBot. |

## Dependencias fiscales que el instalador debe materializar

El extra `contabot` de `pyproject.toml` fija `openpyxl==3.1.5` y
`selenium==4.48.0`, exactamente las versiones exigidas por
`plugins/platforms/telegram/fiscal_runtime.py`. Esto evita que las pruebas de
CCMA/SCT fallen al importar `ccma_workbook` en una instalación limpia.

## Gate exigido

La instalación limpia debe ejecutar la suite fiscal con el entorno que instala
el extra `contabot`; no se acepta como evidencia correrla con el Python global.
La réplica portable deja de ser una rama separada: queda demostrada sólo cuando
el mismo comando de #118 instala y valida ContaBot en servidor y notebook.

Hasta completar el paso 8, el inventario debe marcar como ausentes en
`portal_iva_flow.py` y `agip_ddjj_flow.py` las raíces configurables y la
conexión PostgreSQL por perfil. Esta ausencia es un bloqueo visible, no una
capacidad supuesta.
