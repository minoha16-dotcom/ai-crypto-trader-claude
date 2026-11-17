"""Deterministic, rule-based technical features shared by live scan and backtest."""
import math

from .config import settings


def ema(values, period):
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        raise ValueError("Not enough values for EMA")
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = item * alpha + value * (1 - alpha)
    return value


def rsi(values, period=14):
    """Wilder RSI. A flat series returns the neutral value 50."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) <= period:
        raise ValueError("Not enough values for RSI")
    changes = [b - a for a, b in zip(values[:-1], values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles, period=14):
    if period <= 0:
        raise ValueError("period must be positive")
    if len(candles) <= period:
        raise ValueError("Not enough candles for ATR")
    true_ranges = []
    for previous, current in zip(candles[:-1], candles[1:]):
        high, low, previous_close = float(current[2]), float(current[3]), float(previous[4])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def directional_indicators(candles, period=14):
    """Return Wilder ADX, +DI and -DI from the same smoothed DM/TR series."""
    if period <= 0 or len(candles) <= period * 2:
        raise ValueError("Not enough candles for ADX/DI")
    trs, plus_dm, minus_dm = [], [], []
    for previous, current in zip(candles[:-1], candles[1:]):
        up = float(current[2]) - float(previous[2])
        down = float(previous[3]) - float(current[3])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(
            float(current[2]) - float(current[3]),
            abs(float(current[2]) - float(previous[4])),
            abs(float(current[3]) - float(previous[4])),
        ))

    tr_s = sum(trs[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dx_values = []
    plus_di = minus_di = 0.0
    for index in range(period - 1, len(trs)):
        if index >= period:
            tr_s = tr_s - tr_s / period + trs[index]
            plus_s = plus_s - plus_s / period + plus_dm[index]
            minus_s = minus_s - minus_s / period + minus_dm[index]
        plus_di = 100.0 * plus_s / tr_s if tr_s else 0.0
        minus_di = 100.0 * minus_s / tr_s if tr_s else 0.0
        denominator = plus_di + minus_di
        dx_values.append(
            100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0
        )
    if len(dx_values) < period:
        raise ValueError("Not enough directional movement values for ADX")
    adx_value = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_value = (adx_value * (period - 1) + dx) / period
    return {"adx14": adx_value, "plus_di14": plus_di, "minus_di14": minus_di}


def adx(candles, period=14):
    return directional_indicators(candles, period)["adx14"]


def market_context(candles):
    """Classify a timeframe using its latest supplied (already closed) candle."""
    if len(candles) < 200:
        raise ValueError("At least 200 candles required for EMA200 market context")
    closes = [float(candle[4]) for candle in candles]
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    ema200 = ema(closes, 200) if len(closes) >= 200 else None
    dm = directional_indicators(candles)
    regime = "trending" if dm["adx14"] >= float(settings.signal_min_adx) else "ranging"
    bullish = (
        dm["plus_di14"] > dm["minus_di14"] and ema20 > ema50 and closes[-1] > ema200
    )
    bearish = (
        dm["minus_di14"] > dm["plus_di14"] and ema20 < ema50 and closes[-1] < ema200
    )
    direction = "bullish" if bullish else "bearish" if bearish else "neutral"
    return {
        "regime": regime, "direction": direction,
        "adx14": dm["adx14"], "plus_di14": dm["plus_di14"],
        "minus_di14": dm["minus_di14"], "ema20": ema20,
        "ema50": ema50, "ema200": ema200, "price": closes[-1],
    }


def analyze(candles, volume_ratio_threshold=None):
    """Apply hard eligibility gates first; score only eligible trend candidates."""
    if len(candles) < 50:
        raise ValueError("At least 50 candles required for indicators")
    closes = [float(candle[4]) for candle in candles]
    threshold = (
        float(settings.signal_min_volume_ratio)
        if volume_ratio_threshold is None else float(volume_ratio_threshold)
    )
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("volume_ratio_threshold must be finite and non-negative")

    ema20, ema50, ema200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    strength, volatility, last = rsi(closes), atr(candles), closes[-1]
    dm = directional_indicators(candles)
    volumes = [float(candle[5]) for candle in candles]
    average_volume = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0.0
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 0.0
    atr_pct = volatility / last * 100 if last else 0.0

    if dm["adx14"] < float(settings.signal_min_adx):
        regime = "ranging"
    else:
        regime = "trending"

    bullish = (
        dm["plus_di14"] > dm["minus_di14"]
        and ema20 > ema50
        and ema200 is not None and last > ema200
    )
    bearish = (
        dm["minus_di14"] > dm["plus_di14"]
        and ema20 < ema50
        and ema200 is not None and last < ema200
    )
    direction = "bullish" if bullish else "bearish" if bearish else "neutral"
    reasons = []
    if regime != "trending":
        reasons.append("REGIME_RANGING")
    if direction == "neutral":
        reasons.append("DIRECTION_NEUTRAL" if ema200 is not None else "EMA200_HISTORY_NOT_READY")
    if volume_ratio < threshold:
        reasons.append("VOLUME_BELOW_AVERAGE")
    if atr_pct < float(settings.signal_min_atr_pct):
        reasons.append("ATR_PERCENT_TOO_LOW")

    side = "LONG" if direction == "bullish" else "SHORT" if direction == "bearish" else "WAIT"
    eligible = not reasons
    score = 0
    if eligible:
        # Fixed, transparent ranking weights; scoring cannot override a hard gate.
        directional_rsi = (
            min(100.0, max(0.0, (strength - 50.0) * 2.0))
            if side == "LONG"
            else min(100.0, max(0.0, (50.0 - strength) * 2.0))
        )
        directional_volume = min(100.0, volume_ratio / max(threshold, 0.01) * 50.0)
        trend_strength = min(100.0, max(0.0, (dm["adx14"] - 10.0) * 2.5))
        ema_alignment = 100.0 if (
            (side == "LONG" and ema20 > ema50) or
            (side == "SHORT" and ema20 < ema50)
        ) else 0.0
        price_alignment = 100.0 if (
            (side == "LONG" and last > ema20) or
            (side == "SHORT" and last < ema20)
        ) else 0.0
        score = round(
            trend_strength * 0.25
            + directional_rsi * 0.20
            + directional_volume * 0.15
            + ema_alignment * 0.20
            + price_alignment * 0.20
        )

    stop_distance = max(volatility * 2.0, last * 0.005)
    gross_reward = stop_distance * float(settings.reward_risk_ratio)
    estimated_round_trip_cost = last * (
        2.0 * float(settings.taker_fee_rate)
        + 2.0 * float(settings.slippage_bps) / 10_000.0
    )
    net_reward = gross_reward - estimated_round_trip_cost
    net_risk = stop_distance + estimated_round_trip_cost
    net_rr = net_reward / net_risk if net_risk > 0 else 0.0
    if net_rr < float(settings.signal_min_net_rr):
        reasons.append("NET_REWARD_RISK_BELOW_MINIMUM")
        eligible = False
    if not eligible:
        side = "WAIT"

    stop = last - stop_distance if direction == "bullish" else last + stop_distance
    target_distance = stop_distance * float(settings.reward_risk_ratio)
    target = last + target_distance if direction == "bullish" else last - target_distance
    return {
        "price": last,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi14": strength,
        "atr14": volatility,
        "atr_pct": atr_pct,
        **dm,
        "volume_ratio": volume_ratio,
        "volume_ratio_threshold": threshold,
        "regime": regime,
        "direction": direction,
        "filter_reasons": reasons,
        "trend": direction,
        "score": score,
        "signal": side,
        "stop_distance": stop_distance,
        "planned_stop": stop,
        "planned_target": target,
        "gross_reward_risk": float(settings.reward_risk_ratio),
        "estimated_round_trip_cost": estimated_round_trip_cost,
        "net_reward_risk": net_rr,
    }
