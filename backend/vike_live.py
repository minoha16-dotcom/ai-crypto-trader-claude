"""Live, read-only Vike opportunity producer for Binance USD-M perpetuals.

Feature definitions mirror scripts/vike_v31_research_pipeline.py:
Donchian-20 breakout events on closed 15m bars; closed 1h/4h context only.
No private API keys, account endpoints, orders, or trade execution.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .ml_shadow import SYMBOLS, direction_aligned_features, validate_causal_timestamps

BAR_MS = 900_000
HOUR_MS = 3_600_000
FOUR_HOUR_MS = 14_400_000
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"


class VikeMarketDataError(ValueError):
    pass


def _fetch_klines(symbol: str, interval: str, limit: int) -> list[list]:
    url = f"{BASE_URL}?symbol={symbol}&interval={interval}&limit={limit}"
    request = Request(url, headers={"User-Agent": "AI-Crypto-Trader-Vike-Shadow/1.0"})
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise VikeMarketDataError(f"{symbol} {interval}: public Binance data unavailable") from exc
    if not isinstance(payload, list):
        raise VikeMarketDataError(f"{symbol} {interval}: invalid kline response")
    rows = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 7:
            continue
        try:
            row = [int(item[0]), float(item[1]), float(item[2]), float(item[3]),
                   float(item[4]), float(item[5]), int(item[6])]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(v) for v in row[1:6]):
            continue
        if min(row[1:5]) <= 0 or row[5] < 0:
            continue
        if row[3] > min(row[1], row[4]) or row[2] < max(row[1], row[4]) or row[2] < row[3]:
            continue
        rows.append(row)
    rows.sort(key=lambda x: x[0])
    if len(rows) < 55:
        raise VikeMarketDataError(f"{symbol} {interval}: insufficient valid candles")
    return rows


def _closed(rows: list[list], now_ms: int) -> list[list]:
    # Binance kline close_time is inclusive; only use candles fully closed by now.
    result = [row for row in rows if row[6] < now_ms]
    if len(result) < 55:
        raise VikeMarketDataError("insufficient closed candles after removing forming bar")
    return result


def _rsi_normalized(closes: list[float], end: int, period: int = 14) -> float:
    # Wilder RSI, normalized to [0, 1], matching the research feature contract.
    changes = [closes[j] - closes[j - 1] for j in range(1, end + 1)]
    if len(changes) < period:
        raise VikeMarketDataError("RSI warmup incomplete")
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_gain == 0 and avg_loss == 0:
        return 0.5
    if avg_loss == 0:
        return 1.0
    if avg_gain == 0:
        return 0.0
    return (100.0 - 100.0 / (1.0 + avg_gain / avg_loss)) / 100.0


def _wilder_atr(rows: list[list], end: int, period: int = 14) -> float:
    trs = []
    for j in range(1, end + 1):
        high, low, previous_close = rows[j][2], rows[j][3], rows[j - 1][4]
        trs.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(trs) < period:
        raise VikeMarketDataError("ATR warmup incomplete")
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def _context_returns(rows: list[list], signal_close_ms: int, timeframe_ms: int) -> tuple[float, float, int]:
    eligible = [row for row in rows if row[6] <= signal_close_ms]
    if len(eligible) < 5:
        raise VikeMarketDataError("higher timeframe context warmup incomplete")
    current = eligible[-1]
    if signal_close_ms - current[6] >= timeframe_ms:
        raise VikeMarketDataError("higher timeframe context is stale or not fully closed")
    return current[4] / eligible[-2][4] - 1.0, current[4] / eligible[-5][4] - 1.0, current[6]


def _latest_breakout(symbol: str, bars: list[list], h1: list[list], h4: list[list], now_ms: int) -> dict | None:
    # Donchian-20 event generator, same arm/reset behavior as the frozen research.
    armed = {"LONG": True, "SHORT": True}
    last_event = None
    closes = [row[4] for row in bars]
    for i in range(20, len(bars)):
        bar = bars[i]
        prior = bars[i - 20:i]
        upper = max(row[2] for row in prior)
        lower = min(row[3] for row in prior)
        if lower <= bar[4] <= upper:
            armed = {"LONG": True, "SHORT": True}
        side = "LONG" if bar[4] > upper else "SHORT" if bar[4] < lower else None
        if side is None or not armed[side]:
            continue
        signal_time = bar[6]
        if now_ms - signal_time > 2 * BAR_MS:
            armed[side] = False
            continue
        atr = _wilder_atr(bars, i)
        width = upper - lower
        mean_volume = sum(row[5] for row in prior) / 20.0
        if atr <= 0 or width <= 0:
            armed[side] = False
            continue
        h1r1, h1r4, h1close = _context_returns(h1, signal_time, HOUR_MS)
        h4r1, h4r4, h4close = _context_returns(h4, signal_time, FOUR_HOUR_MS)
        hour = (bar[0] // HOUR_MS) % 24
        raw = {
            "ret_1": bar[4] / closes[i - 1] - 1.0,
            "ret_4": bar[4] / closes[i - 4] - 1.0,
            "ret_16": bar[4] / closes[i - 16] - 1.0,
            "atr_pct": atr / bar[4],
            "donchian_width_pct": width / bar[4],
            "channel_position": (bar[4] - lower) / width,
            "volume_ratio": bar[5] / mean_volume if mean_volume > 0 else 0.0,
            "rsi14": _rsi_normalized(closes, i),
            "hour_sin": math.sin(2 * math.pi * hour / 24.0),
            "hour_cos": math.cos(2 * math.pi * hour / 24.0),
            "htf_1h_ret_1": h1r1, "htf_1h_ret_4": h1r4,
            "htf_4h_ret_1": h4r1, "htf_4h_ret_4": h4r4,
        }
        validate_causal_timestamps(signal_time, signal_time, h1close, h4close)
        direction_aligned_features(raw, side)  # fail closed before returning candidate
        last_event = {
            "symbol": symbol, "side": side, "timeframe": "15m",
            "market_scope": "BINANCE_USDM_PERPETUAL",
            "signal_time_ms": signal_time, "feature_available_time_ms": signal_time,
            "htf_1h_close_time_ms": h1close, "htf_4h_close_time_ms": h4close,
            "raw_features": raw, "price": bar[4], "atr": atr,
            "donchian_upper": upper, "donchian_lower": lower,
            "event_source": "DONCHIAN_20_CLOSED_15M",
        }
        armed[side] = False
    return last_event


def _scan_one(symbol: str) -> dict:
    now_ms = int(time.time() * 1000)
    base = _closed(_fetch_klines(symbol, "15m", 160), now_ms)
    h1 = _closed(_fetch_klines(symbol, "1h", 80), now_ms)
    h4 = _closed(_fetch_klines(symbol, "4h", 80), now_ms)
    candidate = _latest_breakout(symbol, base, h1, h4, now_ms)
    return candidate or {"symbol": symbol, "status": "NO_FRESH_DONCHIAN_EVENT"}


async def scan_vike_candidates(symbols: list[str] | None = None) -> list[dict]:
    selected = [str(s).upper().replace("/", "") for s in (symbols or list(SYMBOLS))]
    if not selected or len(selected) > 8 or any(s not in SYMBOLS for s in selected):
        raise VikeMarketDataError("symbols must be selected from the frozen 8-symbol Vike universe")
    results = await asyncio.gather(*(asyncio.to_thread(_scan_one, symbol) for symbol in selected),
                                  return_exceptions=True)
    output = []
    for symbol, result in zip(selected, results):
        if isinstance(result, Exception):
            output.append({"symbol": symbol, "status": "DATA_UNAVAILABLE", "error": str(result)})
        else:
            output.append(result)
    return output
