"""Deterministic market snapshot image support for LINE cron reports.

This intentionally avoids AI image generation.  The cron agent can emit a small
JSON block in its final response and the delivery layer turns that into a PNG
with stable numeric/ASCII labels.  The user-facing LINE text remains Thai; the
image is a compact dashboard companion.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from gateway.line_stock_chart import _Image, chart_output_dir, chart_public_base_url


_MARKER = "LINE_MARKET_SNAPSHOT:"


@dataclass(frozen=True)
class MarketSnapshotResult:
    image_path: str
    image_url: str
    data: dict[str, Any]


def extract_snapshot_payload(content: str) -> tuple[dict[str, Any] | None, str]:
    """Extract and remove a LINE_MARKET_SNAPSHOT JSON block from content.

    Supported shapes:
      LINE_MARKET_SNAPSHOT: {"bias":"NEUTRAL", ...}
      LINE_MARKET_SNAPSHOT:
      { ... }

    The block is always stripped from the delivered text, even when invalid.
    """
    text = str(content or "")
    idx = text.find(_MARKER)
    if idx < 0:
        return None, text.strip()

    before = text[:idx].rstrip()
    after = text[idx + len(_MARKER):].lstrip()
    json_start = after.find("{")
    if json_start < 0:
        return None, before.strip()

    start = json_start
    depth = 0
    in_string = False
    escape = False
    end = None
    for pos, ch in enumerate(after[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = pos + 1
                break

    if end is None:
        return None, before.strip()

    raw_json = after[start:end]
    remainder = after[end:].strip()
    cleaned = "\n".join(part for part in (before.strip(), remainder) if part).strip()
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None, cleaned
    if not isinstance(payload, dict):
        return None, cleaned
    return payload, cleaned


def payload_is_complete(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if not _text(payload.get("bias")):
        return False
    if not (_text(payload.get("set")) or _text(payload.get("set_level"))):
        return False
    if len(_list(payload.get("drivers"))) < 2:
        return False
    if len(_list(payload.get("sectors"))) < 2:
        return False
    if len(_list(payload.get("watch")) or _list(payload.get("watchlist"))) < 3:
        return False
    if not _text(payload.get("risk")):
        return False
    if not _text(payload.get("tactical")):
        return False
    return True


def render_market_snapshot(
    payload: dict[str, Any],
    *,
    output_dir: Path | str | None = None,
    public_base_url: str | None = None,
) -> MarketSnapshotResult:
    """Render a 1200x720 PNG market snapshot from a complete payload."""
    if not payload_is_complete(payload):
        raise ValueError("market snapshot payload is incomplete")

    outdir = Path(output_dir) if output_dir is not None else chart_output_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    filename = f"thai-market-snapshot-{int(time.time())}.png"
    image_path = outdir / filename
    base_url = public_base_url or chart_public_base_url()

    img = _Image(1200, 720, (15, 23, 42))
    _draw_snapshot(img, payload)
    image_path.write_bytes(img.to_png())
    return MarketSnapshotResult(
        image_path=str(image_path),
        image_url=urljoin(base_url.rstrip("/") + "/", filename),
        data=payload,
    )


def render_snapshot_from_content(content: str) -> tuple[str | None, str]:
    """Extract snapshot JSON from content, render if complete, return URL + text."""
    payload, cleaned = extract_snapshot_payload(content)
    if not payload or not payload_is_complete(payload):
        return None, cleaned
    result = render_market_snapshot(payload)
    return result.image_url, cleaned


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
    return str(value).strip()


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(v) for v in value if _text(v)]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]
    return []


def _ascii(value: Any, limit: int = 28) -> str:
    s = _text(value)
    # Keep digits, Latin labels, punctuation useful for ticker symbols and levels.
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", " ", s).strip().upper()
    return (s[: max(0, limit - 1)].rstrip() + "~") if len(s) > limit else s


def _card(img: _Image, x0: int, y0: int, x1: int, y1: int, border: tuple[int, int, int], fill: tuple[int, int, int]) -> None:
    img.rect(x0, y0, x1, y1, fill, True)
    img.rect(x0, y0, x1, y1, border, False)


def _draw_list(img: _Image, x: int, y: int, title: str, items: list[str], color: tuple[int, int, int], *, max_items: int = 4) -> None:
    img.text(x, y, title, color, 2)
    yy = y + 42
    for i, item in enumerate(items[:max_items], start=1):
        img.text(x, yy, f"{i}. {_ascii(item, 34)}", (226, 232, 240), 2)
        yy += 38


def _draw_snapshot(img: _Image, payload: dict[str, Any]) -> None:
    fg = (226, 232, 240)
    muted = (148, 163, 184)
    blue = (96, 165, 250)
    green = (34, 197, 94)
    red = (248, 113, 113)
    yellow = (250, 204, 21)
    slate = (30, 41, 59)
    slate2 = (22, 33, 55)

    bias = _ascii(payload.get("bias"), 18) or "NEUTRAL"
    bias_color = green if "POS" in bias or "BULL" in bias or "UP" in bias else red if "NEG" in bias or "BEAR" in bias or "DOWN" in bias else yellow
    set_level = _ascii(payload.get("set") or payload.get("set_level"), 18)
    flow = _ascii(payload.get("flow") or payload.get("foreign_flow") or "N/A", 20)
    date = _ascii(payload.get("date") or time.strftime("%Y-%m-%d"), 16)

    img.text(55, 35, "THAI MARKET SNAPSHOT", fg, 3)
    img.text(850, 44, date, muted, 2)
    img.text(55, 76, "DATA-DRIVEN LINE DASHBOARD", muted, 2)

    _card(img, 55, 125, 345, 285, blue, slate)
    img.text(80, 150, "SET", blue, 3)
    img.text(80, 205, set_level, fg, 4)

    _card(img, 365, 125, 655, 285, bias_color, slate)
    img.text(390, 150, "BIAS", bias_color, 3)
    img.text(390, 205, bias, fg, 3)

    _card(img, 675, 125, 1145, 285, yellow, slate)
    img.text(700, 150, "FLOW / MACRO", yellow, 3)
    img.text(700, 210, flow, fg, 2)

    _card(img, 55, 320, 565, 555, blue, slate2)
    _draw_list(img, 80, 350, "KEY DRIVERS", _list(payload.get("drivers")), blue, max_items=4)

    _card(img, 595, 320, 1145, 555, green, slate2)
    sectors = _list(payload.get("sectors"))
    watch = _list(payload.get("watch")) or _list(payload.get("watchlist"))
    _draw_list(img, 620, 350, "SECTOR WATCH", sectors, green, max_items=3)
    img.text(620, 485, "WATCHLIST", yellow, 2)
    img.text(620, 525, " / ".join(_ascii(x, 8) for x in watch[:5]), fg, 2)

    _card(img, 55, 585, 565, 680, red, slate)
    img.text(80, 610, "RISK", red, 2)
    img.text(80, 645, _ascii(payload.get("risk"), 44), fg, 2)

    _card(img, 595, 585, 1145, 680, yellow, slate)
    img.text(620, 610, "TACTICAL VIEW", yellow, 2)
    img.text(620, 645, _ascii(payload.get("tactical"), 46), fg, 2)
