# Disposición de líneas históricas de ContaBot/Lea en Hermes

Este documento cierra la condición documental de Ágora #118. El commit de
integración `3850a2ec826d97bc81a84daa3318500ae8eeac31` conserva el árbol de
`b3fef42f35`; los merges `ours` sólo registran ascendencia. La tabla explica,
con evidencia del árbol activo, por qué el contenido de cada línea no se debe
reaplicar literalmente.

| Línea histórica | Clase | Sustitución o decisión verificable |
|---|---|---|
| `feat/agora-99-fiscal-secrets` | Reemplazada / preservada | No tiene líneas ausentes. `plugins/platforms/telegram/fiscal_credentials.py` conserva el acceso cifrado incorporado por `ff32771d58`. |
| `fix/lea-ccma-sct-713` | Reemplazada | La secuencia funcional quedó superada por `5450a00ad2` (integración CCMA/SCT), `8d2cb0fdd4` (runtime Selenium común), `c13d368831` (selector fiscal compartido) y los fixes posteriores de diagnóstico. El helper eliminado `document_delivery.py` quedó innecesario: `adapter.py` completa `SendResult.delivered_filename`, y `pdf_xlsx_flow.py`/`portal_iva_flow.py` leen ese campo directamente. |
| `review/agora104-sol-725` | Reemplazada | Es una extensión de la línea anterior. Sus diagnósticos quedaron incorporados o superados por `0aa12eb5b7`, `410c003695`, `866b82a908`, `bc79627796` y `469f81704e`; el selector común posterior es `c13d368831`. |
| `lea/agora-104-706` | Reemplazada | La publicación y los importes CCMA quedaron superados por `089d7e496f` y por el flujo activo de `ccma_artifact.py`, `ccma_dispatch.py` y `ccma_workbook.py`. El helper de entrega obsoleto tiene el reemplazo indicado arriba. |
| `lea/agora-104-port-base-3d267c0-706` | Línea Lea / réplica portable | Su configuración portable fue promovida por `3d267c0c0f` y `eb138e2dc2`; el árbol activo conserva `contabot_deployment.py`, `fiscal_runtime.py` y la configuración explícita por perfil. La instalación reproducible de notebook queda dentro del instalador de #118, no como fork Lea separado. |
| `feat/agora-104-lea-menu-021` | Reemplazada | `operational_menu.py` fue absorbido por `adapter.py` y `menu_buttons.py`; la secuencia activa incluye `3d977d6e70` (menú por dominio), `53aa35e78c` (botones comunes) y los ajustes medidos posteriores. `contabot_deployment.py` conserva las rutas configurables. |
| `feat/agora-104-lea-portable` | Línea Lea / réplica portable | `dde67c5b20`, `eb138e2dc2` y `3d267c0c0f` son la base portable. El test histórico `test_contabot_deployment.py` se reemplaza en #118 por el gate del instalador, que debe probar el extra `contabot`, rutas por perfil y una instalación limpia. |
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
