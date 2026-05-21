"""LINE command access policy tests.

LINE has no Telegram-style command menu/buttons, so normal users should be
able to chat and upload images while owner/admin slash commands stay restricted.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


ADMIN_USER_ID = "U9b9381c44b4e53c8a9fc87b0757e1f55"
OTHER_USER_ID = "Uotherlineuser"


def _clear_line_auth_env(monkeypatch) -> None:
    for key in (
        "LINE_ALLOWED_USERS",
        "LINE_COMMAND_ALLOWED_USERS",
        "LINE_ALLOW_ALL_USERS",
        "LINE_COMMAND_ALLOW_ALL_USERS",
        "LINE_ALLOW_GENERAL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(
    text: str,
    *,
    user_id: str = OTHER_USER_ID,
    message_type: MessageType = MessageType.TEXT,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=message_type,
        message_id="line-msg-1",
        source=SessionSource(
            platform=Platform.LINE,
            user_id=user_id,
            chat_id=user_id,
            user_name="line-user",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.LINE: PlatformConfig(enabled=True)}
    )
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.LINE: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._session_run_generation = {}
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    return runner


@pytest.mark.asyncio
async def test_line_non_admin_general_text_is_allowed_but_slash_command_is_denied(monkeypatch):
    _clear_line_auth_env(monkeypatch)
    monkeypatch.setenv("LINE_ALLOW_GENERAL_USERS", "true")
    monkeypatch.setenv("LINE_ALLOWED_USERS", ADMIN_USER_ID)

    runner = _make_runner()
    seen = {}

    async def _capture(event, source, _quick_key, _run_generation):
        seen["text"] = event.text
        seen["user_id"] = source.user_id
        return "agent response"

    runner._handle_message_with_agent = _capture
    runner._handle_commands_command = AsyncMock(
        side_effect=AssertionError("non-admin LINE user reached /commands handler")
    )

    chat_result = await runner._handle_message(_make_event("สวัสดี"))
    command_result = await runner._handle_message(_make_event("/commands"))

    assert chat_result == "agent response"
    assert seen == {"text": "สวัสดี", "user_id": OTHER_USER_ID}
    assert command_result is not None
    assert "ไม่มีสิทธิ์" in command_result
    runner._handle_commands_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_line_non_admin_photo_question_is_allowed(monkeypatch):
    _clear_line_auth_env(monkeypatch)
    monkeypatch.setenv("LINE_ALLOW_GENERAL_USERS", "true")
    monkeypatch.setenv("LINE_ALLOWED_USERS", ADMIN_USER_ID)

    runner = _make_runner()
    seen = {}

    async def _capture(event, source, _quick_key, _run_generation):
        seen["message_type"] = event.message_type
        seen["user_id"] = source.user_id
        return "photo response"

    runner._handle_message_with_agent = _capture

    result = await runner._handle_message(
        _make_event("รูปนี้คืออะไร", message_type=MessageType.PHOTO)
    )

    assert result == "photo response"
    assert seen == {"message_type": MessageType.PHOTO, "user_id": OTHER_USER_ID}


@pytest.mark.asyncio
async def test_line_admin_can_use_gateway_commands(monkeypatch):
    _clear_line_auth_env(monkeypatch)
    monkeypatch.setenv("LINE_ALLOW_GENERAL_USERS", "true")
    monkeypatch.setenv("LINE_ALLOWED_USERS", ADMIN_USER_ID)

    runner = _make_runner()
    runner._handle_commands_command = AsyncMock(return_value="command list")
    runner._handle_message_with_agent = AsyncMock(
        side_effect=AssertionError("admin /commands should not fall through to agent")
    )

    result = await runner._handle_message(
        _make_event("/commands", user_id=ADMIN_USER_ID)
    )

    assert result == "command list"
    runner._handle_commands_command.assert_awaited_once()
    runner._handle_message_with_agent.assert_not_awaited()
