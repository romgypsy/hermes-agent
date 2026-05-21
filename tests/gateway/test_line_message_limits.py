"""Regression tests for LINE 5,000-character outbound text limit."""

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.base import SendResult
from gateway.platforms.line import LineAdapter


def test_line_split_text_keeps_each_part_under_api_limit():
    text = "ก" * 12000

    parts = LineAdapter._split_text_for_line(text)

    assert 1 < len(parts) <= LineAdapter.MAX_TEXT_MESSAGES_PER_SEND
    assert all(len(part) <= LineAdapter.MAX_MESSAGE_LENGTH for part in parts)
    assert parts[0].startswith("[ส่วน 1/")
    assert parts[-1].startswith(f"[ส่วน {len(parts)}/")


def test_line_split_text_caps_over_five_messages_with_thai_note():
    text = "x" * 40000

    parts = LineAdapter._split_text_for_line(text)

    assert len(parts) == LineAdapter.MAX_TEXT_MESSAGES_PER_SEND
    assert all(len(part) <= LineAdapter.MAX_MESSAGE_LENGTH for part in parts)
    assert "ข้อความยาวเกินข้อจำกัด LINE" in parts[-1]


@pytest.mark.asyncio
async def test_line_send_push_uses_split_text_messages(monkeypatch):
    adapter = LineAdapter(PlatformConfig(enabled=True))
    adapter._line_bot_api = type("FakeLineApi", (), {})()
    calls = []

    def fake_push(chat_id, messages):
        calls.append((chat_id, messages))

    adapter._line_bot_api.push_message = fake_push

    result = await adapter.send("Utest", "x" * 12000, metadata={"line_force_push": True})

    assert result.success is True
    assert calls
    chat_id, messages = calls[0]
    assert chat_id == "Utest"
    assert 1 < len(messages) <= LineAdapter.MAX_TEXT_MESSAGES_PER_SEND
    assert all(len(msg.text) <= LineAdapter.MAX_MESSAGE_LENGTH for msg in messages)


@pytest.mark.asyncio
async def test_line_send_image_does_not_append_image_url_to_caption():
    adapter = LineAdapter(PlatformConfig(enabled=True))
    adapter._line_bot_api = type("FakeLineApi", (), {})()
    calls = []

    def fake_push(chat_id, messages):
        calls.append((chat_id, messages))

    adapter._line_bot_api.push_message = fake_push
    image_url = "https://img.clyfe.online/charts/BTC-USD_6m_test.png"
    caption = f"วิเคราะห์กราฟ BTC วันนี้\nไฟล์ PNG โดยตรง: {image_url}\nแนวโน้มยังแกว่งตัว"

    result = await adapter.send_image("Utest", image_url, caption=caption, metadata={"line_force_push": True})

    assert result.success is True
    assert calls
    _, messages = calls[0]
    assert len(messages) == 2
    assert messages[0].original_content_url == image_url
    assert image_url not in messages[1].text
    assert "ไฟล์ PNG" not in messages[1].text
    assert "วิเคราะห์กราฟ BTC วันนี้" in messages[1].text


@pytest.mark.asyncio
async def test_line_delivery_extracts_image_url_and_strips_it_from_text():
    class FakeLineAdapter:
        def __init__(self):
            self.images = []
            self.texts = []

        async def send_image(self, chat_id, image_url, caption=None, metadata=None):
            self.images.append((chat_id, image_url, caption, metadata))
            return SendResult(success=True, message_id="image")

        async def send(self, chat_id, content, metadata=None):
            self.texts.append((chat_id, content, metadata))
            return SendResult(success=True, message_id="text")

    adapter = FakeLineAdapter()
    router = DeliveryRouter(config=None, adapters={Platform.LINE: adapter})
    image_url = "https://img.clyfe.online/charts/SET_BK_6m_test.png"
    content = f"รายงานตลาดไทย\nLINE_IMAGE_URL: {image_url}\nSET ยังแกว่งตัว"

    result = await router._deliver_to_platform(
        DeliveryTarget(platform=Platform.LINE, chat_id="Utest"),
        content,
        metadata={"job_id": "test"},
    )

    assert result["images"][0]["success"] is True
    assert adapter.images[0][1] == image_url
    assert adapter.images[0][2] is None
    assert adapter.texts
    assert image_url not in adapter.texts[0][1]
    assert "LINE_IMAGE_URL" not in adapter.texts[0][1]
    assert "รายงานตลาดไทย" in adapter.texts[0][1]


@pytest.mark.asyncio
async def test_line_send_image_file_publishes_tmp_file_as_public_https_image(monkeypatch, tmp_path):
    import gateway.platforms.line as line_module

    chart_dir = tmp_path / "public-charts"
    chart_dir.mkdir()
    source = tmp_path / "hermes-test.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nlocal-image")

    config = PlatformConfig(enabled=True, extra={"chart_public_base_url": "https://img.clyfe.online/charts"})
    adapter = LineAdapter(config)
    sent = {}

    async def fake_send_image(chat_id, image_url, caption=None, reply_to=None, metadata=None):
        sent.update(
            chat_id=chat_id,
            image_url=image_url,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )
        return SendResult(success=True, message_id="image")

    monkeypatch.setattr(line_module, "chart_output_dir", lambda: chart_dir)
    monkeypatch.setattr(adapter, "send_image", fake_send_image)

    result = await adapter.send_image_file(
        "Utest",
        str(source),
        caption="กราฟ BTC",
        metadata={"line_force_push": True},
    )

    assert result.success is True
    assert sent["chat_id"] == "Utest"
    assert sent["image_url"].startswith("https://img.clyfe.online/charts/hermes-test_")
    assert sent["image_url"].endswith(".png")
    assert sent["caption"] == "กราฟ BTC"
    published = chart_dir / sent["image_url"].rsplit("/", 1)[-1]
    assert published.exists()
    assert published.read_bytes() == source.read_bytes()
