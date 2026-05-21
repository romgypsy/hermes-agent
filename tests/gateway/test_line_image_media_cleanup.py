"""Regression tests for LINE image downloads and post-response cleanup."""

import asyncio
import os
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.platforms.line import LineAdapter
from gateway.session import SessionSource


class FakeImageMessage:
    def __init__(self, message_id="line-msg-1"):
        self.id = message_id


class FakeLineMessageEvent:
    def __init__(self):
        self.message = FakeImageMessage()
        self.source = SimpleNamespace(user_id="U123", group_id=None, room_id=None)
        self.reply_token = "reply-token"
        self.type = "message"


class FakeContentResponse:
    headers = {"Content-Type": "image/jpeg"}

    def iter_content(self, chunk_size=1024):
        yield b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class FakeLineBotApi:
    def __init__(self):
        self.requested_ids = []

    def get_message_content(self, message_id):
        self.requested_ids.append(message_id)
        return FakeContentResponse()


def test_line_image_message_is_downloaded_to_local_media_folder(tmp_path, monkeypatch):
    import gateway.platforms.line as line_module

    monkeypatch.setattr(line_module, "MessageEvent", FakeLineMessageEvent)
    monkeypatch.setattr(line_module, "ImageMessage", FakeImageMessage)
    monkeypatch.setattr(line_module, "TextMessage", type("FakeTextMessage", (), {}))
    monkeypatch.setattr(line_module, "VideoMessage", type("FakeVideoMessage", (), {}))
    monkeypatch.setattr(line_module, "AudioMessage", type("FakeAudioMessage", (), {}))
    monkeypatch.setattr(line_module, "LocationMessage", type("FakeLocationMessage", (), {}))
    monkeypatch.setattr(line_module, "StickerMessage", type("FakeStickerMessage", (), {}))
    monkeypatch.setenv("LINE_MEDIA_DIR", str(tmp_path / "line-media"))

    class FakeParser:
        def __init__(self, secret):
            self.secret = secret

        def parse(self, body, signature):
            return [FakeLineMessageEvent()]

    monkeypatch.setattr(line_module, "WebhookParser", FakeParser)

    adapter = LineAdapter(
        PlatformConfig(
            enabled=True,
            extra={"channel_access_token": "token", "channel_secret": "secret"},
        )
    )
    adapter._webhook_handler = object()
    adapter._line_bot_api = FakeLineBotApi()

    events = adapter.handle_webhook_event("{}", "signature")

    assert len(events) == 1
    event = events[0]
    assert event.message_type == MessageType.PHOTO
    assert event.media_types == ["image/jpeg"]
    assert len(event.media_urls) == 1
    assert os.path.exists(event.media_urls[0])
    assert str(tmp_path / "line-media") in event.media_urls[0]
    assert event.metadata["line_temp_media_paths"] == event.media_urls
    assert adapter._line_bot_api.requested_ids == ["line-msg-1"]


class CleanupAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), platform=Platform.LINE)
        self.sent = []
        self.config.extra = {}

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True)

    async def receive(self):
        return None

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def _keep_typing(self, *args, **kwargs):
        # Wait forever until the processing task cancels us.
        await asyncio.Event().wait()


class PromptingLineAdapter(LineAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(
                enabled=True,
                extra={"channel_access_token": "token", "channel_secret": "secret"},
            )
        )
        self.sent = []
        self.config.extra = {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata or {}})
        return SendResult(success=True)

    async def _keep_typing(self, *args, **kwargs):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_line_photo_without_text_prompts_for_question_and_waits_for_followup(tmp_path):
    media_path = tmp_path / "line-media" / "line-msg-1.jpg"
    media_path.parent.mkdir()
    media_path.write_bytes(b"image")

    adapter = PromptingLineAdapter()
    handled_events = []

    async def handler(event):
        handled_events.append(event)
        assert event.text == "รูปนี้คืออะไร"
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == [str(media_path)]
        assert os.path.exists(media_path)
        return "คำตอบจากรูป"

    adapter.set_message_handler(handler)

    photo_event = MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=SessionSource(platform=Platform.LINE, chat_id="U123", chat_type="dm", user_id="U123"),
        message_id="line-img-1",
    )
    photo_event.media_urls = [str(media_path)]
    photo_event.media_types = ["image/jpeg"]
    photo_event.metadata = {"reply_token": "image-reply", "line_temp_media_paths": [str(media_path)]}

    await adapter.handle_message(photo_event)

    assert handled_events == []
    assert len(adapter.sent) == 1
    assert "ต้องการสอบถามอะไรเกี่ยวกับรูป" in adapter.sent[0]["content"]
    assert adapter.sent[0]["metadata"]["reply_token"] == "image-reply"
    assert media_path.exists()

    text_event = MessageEvent(
        text="รูปนี้คืออะไร",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.LINE, chat_id="U123", chat_type="dm", user_id="U123"),
        message_id="line-text-1",
    )
    text_event.metadata = {"reply_token": "text-reply"}

    await adapter.handle_message(text_event)
    if adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))

    assert len(handled_events) == 1
    assert adapter.sent[-1]["content"] == "คำตอบจากรูป"
    assert not media_path.exists()


@pytest.mark.asyncio
async def test_temp_line_media_is_deleted_after_response_is_sent(tmp_path):
    media_path = tmp_path / "line-media" / "line-msg-1.jpg"
    media_path.parent.mkdir()
    media_path.write_bytes(b"image")

    adapter = CleanupAdapter()

    async def handler(event):
        assert os.path.exists(media_path)
        return "ตอบเรียบร้อย"

    adapter.set_message_handler(handler)
    event = MessageEvent(
        text="ช่วยดูรูปนี้",
        message_type=MessageType.PHOTO,
        source=SessionSource(platform=Platform.LINE, chat_id="U123", chat_type="dm", user_id="U123"),
        message_id="line-msg-1",
    )
    event.media_urls = [str(media_path)]
    event.media_types = ["image/jpeg"]
    event.metadata = {"line_temp_media_paths": [str(media_path)]}

    await adapter._process_message_background(event, "line:U123")

    assert adapter.sent == ["ตอบเรียบร้อย"]
    assert not media_path.exists()
