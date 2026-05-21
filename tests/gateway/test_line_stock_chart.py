"""LINE stock chart command and rendering tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.line import LineAdapter
from gateway.session import SessionSource


def _line_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        message_id="line-msg-1",
        source=SessionSource(
            platform=Platform.LINE,
            user_id="U123",
            chat_id="U123",
            user_name="line-user",
            chat_type="dm",
        ),
    )


def test_parse_stock_chart_command_accepts_thai_and_slash_forms():
    from gateway.line_stock_chart import parse_stock_chart_command

    assert parse_stock_chart_command("/chart AOT 1y") == ("AOT.BK", "1y")
    assert parse_stock_chart_command("กราฟหุ้น ptt 6m") == ("PTT.BK", "6m")
    assert parse_stock_chart_command("/chart BTC-USD 3mo") == ("BTC-USD", "3mo")
    assert parse_stock_chart_command("กราฟ btc") == ("BTC-USD", "6mo")
    assert parse_stock_chart_command("ขอกราฟ btc") == ("BTC-USD", "6mo")
    assert parse_stock_chart_command("ขอกราฟ sol") == ("SOL-USD", "6mo")
    assert parse_stock_chart_command("ขอกราฟ หุ้นอเมริกา asml") == ("ASML", "6mo")
    assert parse_stock_chart_command("ขอกราฟ หุ้นไทย aot") == ("AOT.BK", "6mo")
    assert parse_stock_chart_command("ขอกราฟ asml") == ("ASML.BK", "6mo")
    assert parse_stock_chart_command("ขอกราฟ tsm") == ("TSM.BK", "6mo")
    assert parse_stock_chart_command("วิเคราะห์หุ้น AOT") is None


def test_symbol_candidates_prioritize_user_core_markets(monkeypatch):
    from gateway import line_stock_chart

    monkeypatch.setattr(line_stock_chart, "search_yahoo_symbols", lambda query: ["SOL", "SOL-USD", "SOL.BK"])

    assert line_stock_chart._candidate_symbols_for_retry("SOL.BK", market_hint="crypto")[:2] == ["SOL-USD", "SOL"]
    assert line_stock_chart._candidate_symbols_for_retry("ASML.BK", market_hint="us")[:2] == ["ASML", "ASML.BK"]
    assert line_stock_chart._candidate_symbols_for_retry("AOT", market_hint="thai")[:2] == ["AOT.BK", "AOT"]


def test_build_chart_resolves_global_symbol_when_bare_symbol_was_thai_normalized(monkeypatch, tmp_path):
    from gateway import line_stock_chart

    calls = []

    def fake_fetch(symbol, period):
        calls.append(symbol)
        if symbol == "ASML.BK":
            raise RuntimeError("Yahoo Finance did not return chart data")
        if symbol == "ASML":
            return [
                {"date": "2026-01-01", "close": 700.0, "volume": 1},
                {"date": "2026-01-02", "close": 710.0, "volume": 2},
            ]
        raise AssertionError(f"unexpected symbol {symbol}")

    monkeypatch.setattr(line_stock_chart, "fetch_yahoo_points", fake_fetch)
    monkeypatch.setattr(line_stock_chart, "search_yahoo_symbols", lambda query: ["ASML"])

    result = line_stock_chart.build_stock_chart_for_command(
        "ขอกราฟ asml",
        output_dir=tmp_path,
        public_base_url="https://img.clyfe.online/charts",
    )

    assert calls == ["ASML.BK", "ASML"]
    assert result.symbol == "ASML"
    assert result.image_url.startswith("https://img.clyfe.online/charts/ASML_6mo_")


def test_build_chart_raises_clear_error_when_symbol_lookup_has_no_data(monkeypatch, tmp_path):
    from gateway import line_stock_chart

    monkeypatch.setattr(line_stock_chart, "fetch_yahoo_points", lambda symbol, period: (_ for _ in ()).throw(RuntimeError("no chart data")))
    monkeypatch.setattr(line_stock_chart, "search_yahoo_symbols", lambda query: [])

    with pytest.raises(RuntimeError, match="ไม่พบ symbol"):
        line_stock_chart.build_stock_chart_for_command(
            "ขอกราฟ definitelyunknownsymbol",
            output_dir=tmp_path,
            public_base_url="https://img.clyfe.online/charts",
        )


def test_line_image_caption_strips_public_image_urls():
    from gateway.platforms.line import LineAdapter

    text = LineAdapter._format_image_caption(
        "กราฟ AOT.BK\nเปิดหน้าดูรูป: https://hermes.clyfe.online/chart-view/AOT_BK_1y.png\nไฟล์ PNG โดยตรง: https://img.clyfe.online/charts/AOT_BK_1y.png",
        "https://img.clyfe.online/charts/AOT_BK_1y.png",
    )

    assert "กราฟ AOT.BK" in text
    assert "เปิดหน้าดูรูป:" not in text
    assert "ไฟล์ PNG โดยตรง:" not in text
    assert "chart-view" not in text
    assert "/charts/" not in text


def test_line_chart_raw_route_serves_png_with_inline_no_transform_headers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import gateway.line_webhook as line_webhook

    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    (chart_dir / "AOT_BK_1y.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(line_webhook, "chart_output_dir", lambda: chart_dir)

    app = line_webhook.create_line_webhook_app(adapter=object())
    response = TestClient(app).get("/charts/AOT_BK_1y.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store, max-age=0, no-transform"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_line_chart_embed_route_renders_self_contained_base64_image(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import gateway.line_webhook as line_webhook

    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    (chart_dir / "AOT_BK_1y.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(line_webhook, "chart_output_dir", lambda: chart_dir)

    app = line_webhook.create_line_webhook_app(adapter=object())
    response = TestClient(app).get("/chart-embed/AOT_BK_1y.png")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "data:image/png;base64," in response.text
    assert "iVBORw0KGgo=" in response.text
    assert "PNG bytes: 8" in response.text


def test_line_chart_view_route_renders_html_page_with_image(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import gateway.line_webhook as line_webhook

    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    (chart_dir / "AOT_BK_1y.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(line_webhook, "chart_output_dir", lambda: chart_dir)

    app = line_webhook.create_line_webhook_app(adapter=object())
    response = TestClient(app).get("/chart-view/AOT_BK_1y.png")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<img src="/charts/AOT_BK_1y.png"' in response.text
    assert "AOT_BK_1y.png" in response.text


def test_render_stock_chart_creates_public_png(tmp_path):
    from gateway.line_stock_chart import render_stock_chart

    points = [
        {"date": "2026-01-01", "close": 10.0, "volume": 1000},
        {"date": "2026-01-02", "close": 11.0, "volume": 1300},
        {"date": "2026-01-03", "close": 9.5, "volume": 900},
        {"date": "2026-01-04", "close": 12.0, "volume": 2000},
    ]

    result = render_stock_chart(
        symbol="AOT.BK",
        period="1mo",
        points=points,
        output_dir=tmp_path,
        public_base_url="https://hermes.clyfe.online/charts",
    )

    assert result.symbol == "AOT.BK"
    assert result.image_url.startswith("https://hermes.clyfe.online/charts/")
    assert "AOT_BK" in Path(result.image_path).name
    assert "AOT.BK" not in Path(result.image_path).name
    assert result.image_path.endswith(".png")
    assert Path(result.image_path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "ล่าสุด" in result.caption


@pytest.mark.asyncio
async def test_line_adapter_intercepts_chart_command_and_sends_image(monkeypatch, tmp_path):
    from gateway.line_stock_chart import StockChartResult

    config = PlatformConfig(enabled=True, extra={"chart_public_base_url": "https://example.com/charts"})
    adapter = LineAdapter(config)
    adapter._line_bot_api = SimpleNamespace()

    sent = {}

    async def fake_send_image(chat_id, image_url, caption=None, reply_to=None, metadata=None):
        sent.update(
            chat_id=chat_id,
            image_url=image_url,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )
        return SimpleNamespace(success=True)

    async def fail_super_handle_message(_event):
        raise AssertionError("chart command should not enter generic agent pipeline")

    monkeypatch.setattr(adapter, "send_image", fake_send_image)
    monkeypatch.setattr(
        "gateway.platforms.base.BasePlatformAdapter.handle_message",
        fail_super_handle_message,
    )
    monkeypatch.setattr(
        "gateway.platforms.line.build_stock_chart_for_command",
        lambda text, **kwargs: StockChartResult(
            symbol="AOT.BK",
            period="1y",
            image_path=str(tmp_path / "aot.png"),
            image_url="https://example.com/charts/aot.png",
            caption="กราฟ AOT.BK",
            points_count=10,
        ),
    )

    await adapter.handle_message(_line_event("/chart AOT 1y"))

    assert sent["chat_id"] == "U123"
    assert sent["image_url"] == "https://example.com/charts/aot.png"
    assert sent["caption"] == "กราฟ AOT.BK"
    assert sent["reply_to"] == "line-msg-1"
