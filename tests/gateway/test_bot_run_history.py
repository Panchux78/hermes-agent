from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from plugins.platforms.telegram.bot_run_history import BotRunHistory, RunHandle
from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow


def test_shared_client_starts_batch_attempt_and_preserves_previous_link(tmp_path: Path) -> None:
    async def scenario() -> None:
        history = BotRunHistory(tmp_path, instance_id="00000000-0000-4000-8000-000000000001")
        history.recover_once = AsyncMock()
        history._call = AsyncMock(return_value={
            "status": "OK", "id_corrida": 22, "id_item": 31,
            "intento": 2, "id_corrida_anterior": 19,
        })

        run = await history.start_batch(
            telegram_id=7, batch_id="a" * 32, reference="lote.zip",
        )

        assert run == RunHandle(22, 31, 2, 19)
        arguments = history._call.await_args.args
        assert arguments[0] == "start-batch"
        assert arguments[arguments.index("--batch-id") + 1] == "a" * 32

    asyncio.run(scenario())


def test_shared_client_prepares_one_item_per_pdf_and_closes_separately(tmp_path: Path) -> None:
    async def scenario() -> None:
        history = BotRunHistory(tmp_path, instance_id="00000000-0000-4000-8000-000000000001")
        history._call = AsyncMock(side_effect=[
            {"status": "OK", "items": [
                {"id_item": 1, "referencia": "uno.pdf", "estado": "en_curso"},
                {"id_item": 2, "referencia": "dos.pdf", "estado": "en_curso"},
            ]},
            {"status": "OK", "estado": "fallido"},
            {"status": "OK", "estado": "incompleta"},
        ])
        run = RunHandle(10, 1)

        items = await history.prepare_items(run, ["uno.pdf", "dos.pdf"])
        await history.finish_item(
            run, 1, state="fallido", reason_code="pdf_no_identificado",
            reason_text="El PDF no pudo identificarse.",
            effects=[{"codigo": "convertir", "realizado": False}],
        )
        final = await history.close(run)

        assert [item["referencia"] for item in items] == ["uno.pdf", "dos.pdf"]
        assert final == "incompleta"
        assert history._call.await_args_list[1].args[0] == "finish-item"
        assert history._call.await_args_list[2].args[0] == "close"

    asyncio.run(scenario())


def test_shared_client_records_terminal_rejection_without_starting_executor(tmp_path: Path) -> None:
    async def scenario() -> None:
        history = BotRunHistory(tmp_path, instance_id="00000000-0000-4000-8000-000000000001")
        history._call = AsyncMock(return_value={
            "status": "OK", "id_corrida": 44, "id_item": 45, "nueva": True,
        })

        run = await history.reject(
            telegram_id=7, operation="resumen_bancario_xlsx",
            key_material="mensaje-8", reference="archivo.txt",
            reason_code="archivo_invalido", reason_text="El archivo recibido no es un PDF.",
        )

        assert run == RunHandle(44, 45)
        arguments = history._call.await_args.args
        assert arguments[0] == "reject"
        assert arguments[arguments.index("--reason-code") + 1] == "archivo_invalido"
        assert "mensaje-8" not in arguments

    asyncio.run(scenario())


def test_portal_iva_operations_and_warning_results_remain_distinct() -> None:
    generar = PortalIvaFlow._history_effects("generar", prepared=True)
    presentados = PortalIvaFlow._history_effects("descargar-presentados", prepared=True)
    assert generar[0] == {"codigo": "preparar", "realizado": True}
    assert presentados[0] == {"codigo": "consultar", "realizado": True}
    # #142: números no leídos no es lo mismo que comprobantes sin importar.
    estado, codigo, motivo = PortalIvaFlow._history_outcome(
        "ventas", ["IMPORTACION_NUMEROS_NO_PARSEADOS_ventas"],
    )
    assert (estado, codigo) == ("incompleto", "numeros_importacion_no_leidos_ventas")
    assert "no se pudieron leer sus números" in motivo and "no se pudieron importar" not in motivo
    assert PortalIvaFlow._history_outcome(
        "compras", ["IMPORTACION_COMPROBANTES_NO_IMPORTADOS_compras"],
    )[:2] == ("incompleto", "comprobantes_no_importados_compras")
    assert PortalIvaFlow._history_outcome("compras", [])[0] == "completado"


def test_adapter_injects_one_history_component_into_all_four_flows() -> None:
    source = (Path(__file__).resolve().parents[2] / "plugins/platforms/telegram/adapter.py").read_text()
    assert "bot_run_history = BotRunHistory()" in source
    assert "BatchPdfXlsxFlow(history=bot_run_history)" in source
    assert "PdfXlsxFlow(history=bot_run_history)" in source
    assert "history=bot_run_history" in source


def test_attach_run_registers_the_consolidated_file_once_per_run(tmp_path: Path) -> None:
    """Ágora #148: el consolidado pertenece a la corrida, no a cada PDF."""
    async def scenario() -> None:
        history = BotRunHistory(tmp_path, instance_id="00000000-0000-4000-8000-000000000001")
        history._call = AsyncMock(return_value={"status": "OK", "id_archivo": 7})
        run = RunHandle(22, 31, 1, None)
        output = tmp_path / "consolidado.xlsx"
        output.write_bytes(b"xlsx")

        await history.attach_run(run, contributor_id=5, output=output,
                                 output_relative="estudios/1/20111111112/2026/05/bancos/consolidado.xlsx")
        await history.attach(run, 31, contributor_id=5, output=output,
                             output_relative="estudios/1/20111111112/2026/05/bancos/uno.xlsx", libro="ventas")

        first, second = (call.args for call in history._call.await_args_list)
        assert first[0] == "attach-run" and "--item-id" not in first
        assert first[first.index("--libro") + 1] == "consolidado"
        assert first[first.index("--rol") + 1] == "entregable"
        assert second[0] == "attach" and second[second.index("--libro") + 1] == "ventas"

    asyncio.run(scenario())
