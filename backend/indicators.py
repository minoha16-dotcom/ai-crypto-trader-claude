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


def analyze(candles):
    closes = [float(candle[4]) for candle in candles]
    if len(closes) < 50:
        raise ValueError("At least 50 candles required")
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    strength, volatility, last = rsi(closes), atr(candles), closes[-1]
    trend = "bullish" if last > ema20 > ema50 else "bearish" if last < ema20 < ema50 else "sideways"
    score = (
        (1 if ema20 > ema50 else -1 if ema20 < ema50 else 0)
        + (1 if strength >= 55 else -1 if strength <= 45 else 0)
        + (1 if last > ema20 else -1 if last < ema20 else 0)
    )
    signal = "LONG" if score >= 2 else "SHORT" if score <= -2 else "WAIT"
    return {
        "price": last, "ema20": ema20, "ema50": ema50, "rsi14": strength,
        "atr14": volatility, "trend": trend, "score": score, "signal": signal,
        "stop_distance": max(volatility * 2, last * 0.005),
    }
