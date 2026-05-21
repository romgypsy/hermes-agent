"""Regression tests for LINE immediate ack and numeric /model selection."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class DummyAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return None


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._model_picker_choices = {}
    return runner


def _make_line_event(text="hello"):
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.LINE,
            chat_id="Ulineuser",
            chat_type="dm",
            user_id="Ulineuser",
        ),
    )
    event.metadata = {"reply_token": "reply-token"}
    return event


@pytest.mark.asyncio
async def test_line_processing_ack_uses_force_push_metadata():
    runner = _make_runner()
    adapter = DummyAdapter()
    event = _make_line_event("ช่วยเช็คระบบ")
    runner.adapters[Platform.LINE] = adapter

    sent = await runner._send_processing_ack_if_needed(event, event.source)

    assert sent is True
    assert adapter.sent
    assert "รับทราบ" in adapter.sent[0]["content"]
    assert adapter.sent[0]["metadata"]["line_force_push"] is True


@pytest.mark.asyncio
async def test_line_processing_ack_skips_model_command():
    runner = _make_runner()
    adapter = DummyAdapter()
    event = _make_line_event("/model")
    runner.adapters[Platform.LINE] = adapter

    sent = await runner._send_processing_ack_if_needed(event, event.source)

    assert sent is False
    assert adapter.sent == []


def test_numbered_model_choice_resolves_to_cached_provider_choice():
    runner = _make_runner()
    event = _make_line_event("/model 2")
    session_key = runner._session_key_for_source(event.source)
    runner._model_picker_choices[session_key] = {
        "1": {"model": "alpha", "provider": "openai-codex"},
        "2": {"model": "beta", "provider": "openai-codex"},
    }

    model_input, explicit_provider = runner._resolve_numbered_model_choice(
        event,
        model_input="2",
        explicit_provider=None,
    )

    assert model_input == "beta"
    assert explicit_provider == "openai-codex"


@pytest.mark.asyncio
async def test_line_processing_ack_accepts_string_message_type_and_platform():
    runner = _make_runner()
    adapter = DummyAdapter()
    event = _make_line_event("ช่วยเช็คระบบ")
    event.message_type = "text"
    event.source.platform = "line"
    runner.adapters["line"] = adapter

    sent = await runner._send_processing_ack_if_needed(event, event.source)

    assert sent is True
    assert adapter.sent[0]["metadata"]["line_force_push"] is True
    assert adapter.sent[0]["metadata"]["skip_reply_token"] is True
    assert event.metadata["line_processing_ack_sent"] is True


@pytest.mark.asyncio
async def test_line_model_numeric_selection_persists_global_by_default(tmp_path, monkeypatch):
    import yaml
    from hermes_cli.model_switch import ModelSwitchResult
    import gateway.run as gateway_run
    import hermes_cli.model_switch as model_switch

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "old-model", "provider": "old-provider"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})

    calls = []

    def fake_switch_model(**kwargs):
        calls.append(kwargs)
        return ModelSwitchResult(
            success=True,
            new_model="beta",
            target_provider="openai-codex",
            provider_label="OpenAI Codex",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            api_mode="openai",
        )

    monkeypatch.setattr(model_switch, "switch_model", fake_switch_model)

    runner = _make_runner()
    event = _make_line_event("/model 2")
    session_key = runner._session_key_for_source(event.source)
    runner._model_picker_choices[session_key] = {
        "2": {"model": "beta", "provider": "openai-codex"},
    }

    result = await runner._handle_model_command(event)

    assert calls
    assert calls[0]["raw_input"] == "beta"
    assert calls[0]["explicit_provider"] == "openai-codex"
    assert calls[0]["is_global"] is True
    assert "Saved to config.yaml" in result
