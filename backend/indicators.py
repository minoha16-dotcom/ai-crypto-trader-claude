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
    """Wilder ATR over OHLCV candles [timestamp, open, high, low, close, volume]."""
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


def adx(candles, period=14):
    """Wilder ADX calculated from OHLC candles."""
    if period <= 0 or len(candles) <= period * 2:
        raise ValueError("Not enough candles for ADX")
    trs, plus_dm, minus_dm = [], [], []
    for previous, current in zip(candles[:-1], candles[1:]):
        up = float(current[2]) - float(previous[2])
        down = float(previous[3]) - float(current[3])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(float(current[2])-float(current[3]),
                       abs(float(current[2])-float(previous[4])),
                       abs(float(current[3])-float(previous[4]))))
    tr_s, plus_s, minus_s = sum(trs[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
    dx_values = []
    for i in range(period, len(trs)):
        tr_s = tr_s - tr_s / period + trs[i]
        plus_s = plus_s - plus_s / period + plus_dm[i]
        minus_s = minus_s - minus_s / period + minus_dm[i]
        plus_di = 100 * plus_s / tr_s if tr_s else 0.0
        minus_di = 100 * minus_s / tr_s if tr_s else 0.0
        denominator = plus_di + minus_di
        dx_values.append(100 * abs(plus_di-minus_di) / denominator if denominator else 0.0)
    if len(dx_values) < period:
        raise ValueError("Not enough directional movement values for ADX")
    value = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        value = (value * (period - 1) + dx) / period
    return value


def analyze(candles, volume_ratio_threshold=None):
    closes = [float(candle[4]) for candle in candles]
    if len(closes) < 50:
        raise ValueError("At least 50 candles required")
    threshold = (
        float(settings.signal_min_volume_ratio)
        if volume_ratio_threshold is None else float(volume_ratio_threshold)
    )
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("volume_ratio_threshold must be finite and non-negative")

    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    strength, volatility, last = rsi(closes), atr(candles), closes[-1]
    trend = "bullish" if last > ema20 > ema50 else "bearish" if last < ema20 < ema50 else "sideways"
    score = (
        (1 if ema20 > ema50 else -1 if ema20 < ema50 else 0)
        + (1 if strength >= 55 else -1 if strength <= 45 else 0)
        + (1 if last > ema20 else -1 if last < ema20 else 0)
    )
    signal = "LONG" if score >= 2 else "SHORT" if score <= -2 else "WAIT"
    trend_strength = adx(candles)
    volumes = [float(candle[5]) for candle in candles]
    average_volume = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0.0
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 0.0
    atr_pct = volatility / last * 100 if last else 0.0
    filter_reasons = []
    if trend_strength < float(settings.signal_min_adx):
        filter_reasons.append("ADX_BELOW_THRESHOLD")
    if volume_ratio < threshold:
        filter_reasons.append("VOLUME_BELOW_AVERAGE")
    if atr_pct < float(settings.signal_min_atr_pct):
        filter_reasons.append("ATR_PERCENT_TOO_LOW")
    if filter_reasons:
        signal = "WAIT"
    return {
        "price": last, "ema20": ema20, "ema50": ema50, "rsi14": strength,
        "atr14": volatility, "atr_pct": atr_pct, "adx14": trend_strength,
        "volume_ratio": volume_ratio, "volume_ratio_threshold": threshold,
        "filter_reasons": filter_reasons,
        "trend": trend, "score": score, "signal": signal,
        "stop_distance": max(volatility * 2, last * 0.005),
    }
