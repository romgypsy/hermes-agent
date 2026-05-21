"""LINE stock chart command support.

This module is intentionally dependency-light: it uses Yahoo's public chart JSON
endpoint and a tiny stdlib PNG renderer so the gateway can create LINE-compatible
PNG charts without requiring pandas/matplotlib/yfinance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote, urljoin
import json
import math
import os
import re
import struct
import time
import urllib.request
import zlib


_ALLOWED_PERIODS = {
    "1d": ("1d", "5m"),
    "5d": ("5d", "15m"),
    "1mo": ("1mo", "1d"),
    "1m": ("1mo", "1d"),
    "3mo": ("3mo", "1d"),
    "3m": ("3mo", "1d"),
    "6mo": ("6mo", "1d"),
    "6m": ("6mo", "1d"),
    "1y": ("1y", "1d"),
    "2y": ("2y", "1wk"),
    "5y": ("5y", "1wk"),
}

_CRYPTO_ALIASES = {
    "BTC": "BTC-USD",
    "BITCOIN": "BTC-USD",
    "ETH": "ETH-USD",
    "ETHEREUM": "ETH-USD",
    "SOL": "SOL-USD",
    "SOLANA": "SOL-USD",
    "XRP": "XRP-USD",
    "BNB": "BNB-USD",
    "ADA": "ADA-USD",
    "DOGE": "DOGE-USD",
    "AVAX": "AVAX-USD",
    "LINK": "LINK-USD",
    "DOT": "DOT-USD",
    "MATIC": "MATIC-USD",
    "POL": "POL-USD",
    "TRX": "TRX-USD",
    "TON": "TON-USD",
    "SUI": "SUI-USD",
    "LTC": "LTC-USD",
    "BCH": "BCH-USD",
    "XLM": "XLM-USD",
    "HBAR": "HBAR-USD",
    "NEAR": "NEAR-USD",
    "APT": "APT-USD",
    "ARB": "ARB-USD",
    "OP": "OP-USD",
}

_MARKET_HINT_PATTERNS = {
    "crypto": ("crypto", "คริปโต", "เหรียญ", "coin"),
    "thai": ("หุ้นไทย", "ไทย", "set", "mai"),
    "us": ("หุ้นอเมริกา", "หุ้นเมกา", "อเมริกา", "usa", "us stock", "nasdaq", "nyse"),
}


@dataclass(frozen=True)
class StockChartResult:
    symbol: str
    period: str
    image_path: str
    image_url: str
    caption: str
    points_count: int


@dataclass(frozen=True)
class ChartCommand:
    symbol: str
    period: str
    market_hint: Optional[str] = None
    query: str = ""


def _detect_market_hint(text: str) -> Optional[str]:
    lowered = (text or "").strip().lower()
    for hint, patterns in _MARKET_HINT_PATTERNS.items():
        if any(pattern in lowered for pattern in patterns):
            return hint
    return None


def _strip_market_hint_words(text: str) -> str:
    cleaned = text or ""
    replacements = sorted({p for patterns in _MARKET_HINT_PATTERNS.values() for p in patterns}, key=len, reverse=True)
    for word in replacements:
        cleaned = re.sub(re.escape(word), " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_chart_request(text: str) -> Optional[ChartCommand]:
    """Parse LINE chart commands with optional market hints."""
    raw = (text or "").strip()
    if not raw:
        return None
    market_hint = _detect_market_hint(raw)
    parse_text = _strip_market_hint_words(raw)
    pattern = re.compile(
        r"^(?:/(?:chart|stockchart|กราฟ)|(?:ขอ|ช่วย(?:ดู|ทำ|สร้าง)?|อยากดู)?\s*กราฟ(?:หุ้น|ราคา)?)\s+([A-Za-z0-9._=\-^]+)(?:\s+([A-Za-z0-9]+))?\s*$",
        re.IGNORECASE,
    )
    m = pattern.match(parse_text)
    if not m:
        return None
    query = m.group(1)
    symbol = normalize_symbol(query, market_hint=market_hint)
    period = normalize_period(m.group(2) or "6mo")
    return ChartCommand(symbol=symbol, period=period, market_hint=market_hint, query=query.upper())


def parse_stock_chart_command(text: str) -> Optional[tuple[str, str]]:
    """Parse LINE stock/crypto chart commands.

    Supported examples:
    - /chart AOT 1y
    - /stockchart PTT 6m
    - กราฟหุ้น AOT 1y
    - กราฟ BTC 3mo
    - ขอกราฟ btc
    """
    command = parse_chart_request(text)
    if not command:
        return None
    return command.symbol, command.period


def normalize_period(period: str) -> str:
    key = (period or "6mo").strip().lower()
    if key in _ALLOWED_PERIODS:
        return key
    return "6mo"


def normalize_symbol(symbol: str, market_hint: Optional[str] = None) -> str:
    s = re.sub(r"[^A-Za-z0-9._=\-^]", "", (symbol or "").strip()).upper()
    if not s:
        raise ValueError("missing symbol")

    # Friendly aliases users commonly type in LINE.
    aliases = {
        **_CRYPTO_ALIASES,
        "XAU": "GC=F",
        "GOLD": "GC=F",
        "ทอง": "GC=F",
        "SET": "^SET.BK",
    }
    if s in aliases:
        return aliases[s]

    # Explicit market hints override the Thai-default behavior.
    if market_hint == "crypto" and "." not in s and "-" not in s and "=" not in s and not s.startswith("^"):
        return f"{s}-USD"
    if market_hint == "us" and "." not in s and "-" not in s and "=" not in s and not s.startswith("^"):
        return s
    if market_hint == "thai" and "." not in s and "-" not in s and "=" not in s and not s.startswith("^"):
        return f"{s}.BK"

    # Thai SET symbols in Yahoo Finance use .BK. Do not add for obvious global
    # instruments/indexes/FX/crypto that already carry Yahoo suffix syntax.
    if "." not in s and "-" not in s and "=" not in s and not s.startswith("^"):
        s = f"{s}.BK"
    return s


def chart_output_dir() -> Path:
    configured = os.getenv("LINE_CHART_OUTPUT_DIR") or os.getenv("HERMES_PUBLIC_CHART_DIR")
    if configured:
        path = Path(configured).expanduser()
    else:
        path = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser() / "public" / "charts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def chart_public_base_url(configured: Optional[str] = None) -> str:
    base = configured or os.getenv("LINE_CHART_PUBLIC_BASE_URL") or "https://hermes.clyfe.online/charts"
    return base.rstrip("/") + "/"


def fetch_yahoo_points(symbol: str, period: str, timeout: int = 20) -> list[dict]:
    yahoo_period, interval = _ALLOWED_PERIODS.get(period, (period, "1d"))
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol, safe='')}?range={quote(yahoo_period)}&interval={quote(interval)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes LINE Stock Chart/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError("Yahoo Finance did not return chart data")
    timestamps = result.get("timestamp") or []
    quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_data.get("close") or []
    volumes = quote_data.get("volume") or []
    points: list[dict] = []
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        points.append({
            "date": dt,
            "close": float(close),
            "volume": int(volumes[i] or 0) if i < len(volumes) else 0,
        })
    if len(points) < 2:
        raise RuntimeError("not enough price points to render chart")
    return points


def search_yahoo_symbols(query: str, timeout: int = 12, limit: int = 8) -> list[str]:
    """Return Yahoo Finance symbols that best match a user query.

    Yahoo's chart endpoint needs exchange-qualified symbols for many markets.
    The search endpoint lets LINE users type natural/bare symbols such as ASML,
    TSM, 7203, NESN, etc. without us hardcoding every exchange suffix.
    """
    q = re.sub(r"[^A-Za-z0-9._=\-^ ]", "", (query or "").strip())
    if not q:
        return []
    url = (
        "https://query1.finance.yahoo.com/v1/finance/search"
        f"?q={quote(q, safe='')}&quotesCount={int(limit)}&newsCount=0&enableFuzzyQuery=true"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes LINE Symbol Search/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    symbols: list[str] = []
    seen: set[str] = set()
    for item in payload.get("quotes") or []:
        symbol = str(item.get("symbol") or "").strip().upper()
        quote_type = str(item.get("quoteType") or "").upper()
        if not symbol or symbol in seen:
            continue
        if quote_type and quote_type not in {"EQUITY", "ETF", "INDEX", "MUTUALFUND", "CURRENCY", "CRYPTOCURRENCY", "FUTURE"}:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _symbol_lookup_query(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s.endswith(".BK"):
        return s[:-3]
    if s.endswith("-USD"):
        return s[:-4]
    return s


def _candidate_symbols_for_retry(symbol: str, market_hint: Optional[str] = None) -> list[str]:
    query = _symbol_lookup_query(symbol)
    candidates: list[str] = []

    if market_hint == "crypto" and query:
        if query in _CRYPTO_ALIASES:
            candidates.append(_CRYPTO_ALIASES[query])
        elif "." not in query and "-" not in query and "=" not in query and not query.startswith("^"):
            candidates.append(f"{query}-USD")
        candidates.append(query)
    elif market_hint == "us" and query:
        candidates.append(query)
        candidates.append(symbol)
    elif market_hint == "thai" and query:
        if "." not in query and "-" not in query and "=" not in query and not query.startswith("^"):
            candidates.append(f"{query}.BK")
        candidates.append(query)
    elif query and query != symbol:
        candidates.append(query)

    candidates.extend(search_yahoo_symbols(query or symbol))
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        candidate = str(candidate or "").strip().upper()
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def resolve_symbol_points(symbol: str, period: str, market_hint: Optional[str] = None) -> tuple[str, list[dict]]:
    """Fetch chart data, dynamically resolving mistyped/local-default symbols.

    Existing Thai behavior normalizes bare symbols to .BK.  If that fails (e.g.
    ASML -> ASML.BK), retry the bare query and Yahoo search results so global
    symbols work without a hardcoded allowlist.
    """
    errors: list[str] = []
    try:
        return symbol, fetch_yahoo_points(symbol, period)
    except Exception as exc:
        errors.append(f"{symbol}: {exc}")

    for candidate in _candidate_symbols_for_retry(symbol, market_hint=market_hint):
        try:
            return candidate, fetch_yahoo_points(candidate, period)
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
            continue

    query = _symbol_lookup_query(symbol)
    raise RuntimeError(f"ไม่พบ symbol หรือไม่มีข้อมูลราคาสำหรับ {query or symbol}")


def build_stock_chart_for_command(
    text: str,
    *,
    output_dir: Optional[Path | str] = None,
    public_base_url: Optional[str] = None,
) -> Optional[StockChartResult]:
    command = parse_chart_request(text)
    if not command:
        return None
    resolved_symbol, points = resolve_symbol_points(command.symbol, command.period, market_hint=command.market_hint)
    return render_stock_chart(
        symbol=resolved_symbol,
        period=command.period,
        points=points,
        output_dir=Path(output_dir) if output_dir else chart_output_dir(),
        public_base_url=chart_public_base_url(public_base_url),
    )


def render_stock_chart(
    *,
    symbol: str,
    period: str,
    points: list[dict],
    output_dir: Path | str,
    public_base_url: str,
) -> StockChartResult:
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    safe_symbol = re.sub(r"[^A-Za-z0-9_=-]+", "_", symbol).strip("_") or "chart"
    filename = f"{safe_symbol}_{period}_{int(time.time())}.png"
    image_path = outdir / filename
    width, height = 1200, 720
    img = _Image(width, height, (18, 24, 38))
    _draw_chart(img, symbol, period, points)
    image_path.write_bytes(img.to_png())

    first = float(points[0]["close"])
    last = float(points[-1]["close"])
    change = last - first
    pct = (change / first * 100.0) if first else 0.0
    high = max(float(p["close"]) for p in points)
    low = min(float(p["close"]) for p in points)
    direction = "บวก" if change >= 0 else "ลบ"
    caption = (
        f"กราฟ {symbol} ({period})\n"
        f"ล่าสุด: {last:,.2f} | เปลี่ยนแปลง: {change:+,.2f} ({pct:+.2f}%)\n"
        f"ช่วงกราฟ: สูงสุด {high:,.2f} / ต่ำสุด {low:,.2f}\n"
        f"ภาพรวมระยะนี้: {direction}\n"
        "หมายเหตุ: เป็นข้อมูลเพื่อเฝ้าดู ไม่ใช่คำแนะนำการลงทุนส่วนบุคคล"
    )
    return StockChartResult(
        symbol=symbol,
        period=period,
        image_path=str(image_path),
        image_url=urljoin(public_base_url.rstrip("/") + "/", filename),
        caption=caption,
        points_count=len(points),
    )


class _Image:
    def __init__(self, width: int, height: int, bg: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = bytearray(bg * width * height)

    def set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 3
            self.pixels[i:i+3] = bytes(color)

    def line(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], width: int = 1) -> None:
        dx = abs(x1 - x0); sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0); sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            r = max(0, width // 2)
            for ox in range(-r, r + 1):
                for oy in range(-r, r + 1):
                    self.set(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy; x0 += sx
            if e2 <= dx:
                err += dx; y0 += sy

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], fill: bool = False) -> None:
        if fill:
            for y in range(min(y0, y1), max(y0, y1) + 1):
                for x in range(min(x0, x1), max(x0, x1) + 1):
                    self.set(x, y, color)
        else:
            self.line(x0, y0, x1, y0, color)
            self.line(x1, y0, x1, y1, color)
            self.line(x1, y1, x0, y1, color)
            self.line(x0, y1, x0, y0, color)

    def text(self, x: int, y: int, text: str, color: tuple[int, int, int], scale: int = 2) -> None:
        cx = x
        for ch in text.upper():
            glyph = _FONT.get(ch, _FONT.get(" "))
            for row, bits in enumerate(glyph):
                for col in range(5):
                    if bits & (1 << (4 - col)):
                        self.rect(cx + col * scale, y + row * scale, cx + (col + 1) * scale - 1, y + (row + 1) * scale - 1, color, True)
            cx += 6 * scale

    def to_png(self) -> bytes:
        raw = bytearray()
        row_bytes = self.width * 3
        for y in range(self.height):
            raw.append(0)
            start = y * row_bytes
            raw.extend(self.pixels[start:start + row_bytes])
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def _draw_chart(img: _Image, symbol: str, period: str, points: list[dict]) -> None:
    fg = (226, 232, 240); muted = (100, 116, 139); grid = (51, 65, 85)
    green = (34, 197, 94); red = (248, 113, 113); blue = (96, 165, 250); yellow = (250, 204, 21)
    left, top, right, bottom = 90, 90, 1140, 570
    img.text(70, 35, f"{symbol} {period} STOCK CHART", fg, 3)
    img.text(850, 45, "HERMES LINE", muted, 2)
    img.rect(left, top, right, bottom, grid)
    closes = [float(p["close"]) for p in points]
    lo, hi = min(closes), max(closes)
    pad = (hi - lo) * 0.08 or max(abs(hi) * 0.02, 1.0)
    lo -= pad; hi += pad
    for i in range(6):
        y = top + int((bottom - top) * i / 5)
        img.line(left, y, right, y, grid)
        val = hi - (hi - lo) * i / 5
        img.text(10, y - 8, f"{val:.2f}", muted, 1)
    coords = []
    n = len(closes)
    for i, c in enumerate(closes):
        x = left + int((right - left) * i / max(n - 1, 1))
        y = bottom - int((bottom - top) * (c - lo) / (hi - lo))
        coords.append((x, y))
    color = green if closes[-1] >= closes[0] else red
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        img.line(x0, y0, x1, y1, color, 3)
    # 20-period moving average if enough data
    if n >= 20:
        ma = []
        for i in range(n):
            start = max(0, i - 19)
            ma.append(sum(closes[start:i+1]) / (i - start + 1))
        ma_coords = []
        for i, c in enumerate(ma):
            x = left + int((right - left) * i / max(n - 1, 1))
            y = bottom - int((bottom - top) * (c - lo) / (hi - lo))
            ma_coords.append((x, y))
        for (x0, y0), (x1, y1) in zip(ma_coords, ma_coords[1:]):
            img.line(x0, y0, x1, y1, blue, 2)
        img.text(980, 600, "MA20", blue, 2)
    img.text(left, 600, str(points[0].get("date", "")), muted, 2)
    img.text(right - 140, 600, str(points[-1].get("date", "")), muted, 2)
    change = closes[-1] - closes[0]
    pct = change / closes[0] * 100 if closes[0] else 0
    img.text(left, 640, f"LAST {closes[-1]:.2f}  CHG {change:+.2f} ({pct:+.2f}%)", yellow, 2)
    img.text(left, 675, "FOR WATCHLIST ONLY - NOT INVESTMENT ADVICE", muted, 2)


_FONT = {
    " ": [0,0,0,0,0,0,0], "-": [0,0,0,31,0,0,0], ".": [0,0,0,0,0,12,12], "+": [0,4,4,31,4,4,0], "%": [17,2,4,8,17,0,0], "(": [2,4,8,8,8,4,2], ")": [8,4,2,2,2,4,8],
    "0": [14,17,19,21,25,17,14], "1": [4,12,4,4,4,4,14], "2": [14,17,1,2,4,8,31], "3": [30,1,1,14,1,1,30], "4": [2,6,10,18,31,2,2], "5": [31,16,16,30,1,1,30], "6": [6,8,16,30,17,17,14], "7": [31,1,2,4,8,8,8], "8": [14,17,17,14,17,17,14], "9": [14,17,17,15,1,2,12],
    "A": [14,17,17,31,17,17,17], "B": [30,17,17,30,17,17,30], "C": [14,17,16,16,16,17,14], "D": [30,17,17,17,17,17,30], "E": [31,16,16,30,16,16,31], "F": [31,16,16,30,16,16,16], "G": [14,17,16,23,17,17,14], "H": [17,17,17,31,17,17,17], "I": [14,4,4,4,4,4,14], "J": [7,2,2,2,18,18,12], "K": [17,18,20,24,20,18,17], "L": [16,16,16,16,16,16,31], "M": [17,27,21,21,17,17,17], "N": [17,25,21,19,17,17,17], "O": [14,17,17,17,17,17,14], "P": [30,17,17,30,16,16,16], "Q": [14,17,17,17,21,18,13], "R": [30,17,17,30,20,18,17], "S": [15,16,16,14,1,1,30], "T": [31,4,4,4,4,4,4], "U": [17,17,17,17,17,17,14], "V": [17,17,17,17,17,10,4], "W": [17,17,17,21,21,21,10], "X": [17,17,10,4,10,17,17], "Y": [17,17,10,4,4,4,4], "Z": [31,1,2,4,8,16,31],
}
