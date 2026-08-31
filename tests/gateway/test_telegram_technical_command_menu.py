import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_technical_menu_clears_general_scopes_and_registers_model_for_one_chat(monkeypatch):
    async def scenario():
        import telegram

        class _Command:
            def __init__(self, command, description):
                self.command = command
                self.description = description

        class _Scope:
            def __init__(self, chat_id=None):
                self.chat_id = chat_id

        for name in (
            "BotCommandScopeDefault",
            "BotCommandScopeAllPrivateChats",
            "BotCommandScopeAllGroupChats",
            "BotCommandScopeChat",
        ):
            scope = type(name, (_Scope,), {})
            monkeypatch.setattr(telegram, name, scope)
        monkeypatch.setattr(telegram, "BotCommand", _Command)

        adapter = object.__new__(TelegramAdapter)
        adapter.platform = Platform.TELEGRAM
        adapter.config = SimpleNamespace(extra={"technical_menu_user_id": "1234569474"})
        adapter._bot = SimpleNamespace(
            delete_my_commands=AsyncMock(),
            set_my_commands=AsyncMock(),
        )

        with patch("hermes_cli.commands.telegram_menu_max_commands", return_value=60), patch(
            "hermes_cli.commands.telegram_menu_commands",
            return_value=(
                [
                    ("model", "Cambiar modelo de esta sesión"),
                    ("restart", "Reiniciar gateway"),
                ],
                0,
            ),
        ) as menu_commands:
            await adapter._register_technical_command_menu()

        deleted_scopes = [
            call.kwargs["scope"].__class__.__name__
            for call in adapter._bot.delete_my_commands.await_args_list
        ]
        assert deleted_scopes == [
            "BotCommandScopeDefault",
            "BotCommandScopeAllPrivateChats",
            "BotCommandScopeAllGroupChats",
        ]
        adapter._bot.set_my_commands.assert_awaited_once()
        args, kwargs = adapter._bot.set_my_commands.call_args
        assert [command.command for command in args[0]] == ["model", "restart"]
        assert kwargs["scope"].__class__.__name__ == "BotCommandScopeChat"
        assert kwargs["scope"].chat_id == 1234569474
        menu_commands.assert_called_once_with(max_commands=60)

    asyncio.run(scenario())
