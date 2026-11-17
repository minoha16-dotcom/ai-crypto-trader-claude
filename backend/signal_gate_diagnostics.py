"""Per-candle LONG gate counts and pairwise interaction diagnostics."""
from bisect import bisect_right
from itertools import combinations

from .data_validation import timeframe_milliseconds
from .indicators import analyze, market_context


def long_gate_diagnostics(candles, timeframe, higher_timeframe_candles=None,
                          higher_timeframe="4h", require_mtf_confirmation=True,
                          start_index=199, signal_overrides=None):
    """Count LONG gate pass/fail and pairwise intersections over the scored window."""
    gates = ("di_bullish", "ema20_above_ema50", "price_above_ema200",
             "adx_pass", "mtf_bullish")
    totals = {"observations": 0, "directional_long": 0, "qualified_long": 0}
    counts = {name: {"pass": 0, "fail": 0, "not_applicable": 0} for name in gates}
    pairwise = {
        f"{left}&{right}": {"both_pass": 0, "left_pass_right_fail": 0,
                            "left_fail_right_pass": 0, "both_fail": 0}
        for left, right in combinations(gates[:4], 2)
    }
    interval_ms = timeframe_milliseconds(timeframe)
    higher_interval_ms = timeframe_milliseconds(higher_timeframe)
    higher_end_times = [
        int(float(row[0])) + higher_interval_ms
        for row in (higher_timeframe_candles or [])
    ]
    first = max(199, int(start_index))
    for index in range(first, len(candles)):
        metrics = analyze(candles[:index + 1], signal_overrides=signal_overrides)
        raw = metrics.get("long_gate_diagnostics") or {}
        values = {
            "di_bullish": bool(raw.get("di_bullish")),
            "ema20_above_ema50": bool(raw.get("ema20_above_ema50")),
            "price_above_ema200": bool(raw.get("price_above_ema200")),
            "adx_pass": bool(raw.get("adx_pass")),
        }
        mtf_applicable = timeframe == "15m" and require_mtf_confirmation
        if mtf_applicable:
            close_ms = int(float(candles[index][0])) + interval_ms
            eligible = bisect_right(higher_end_times, close_ms)
            if eligible < 200:
                values["mtf_bullish"] = None
            else:
                context = market_context(
                    higher_timeframe_candles[:eligible],
                    min_adx=(signal_overrides or {}).get("adx_min"),
                )
                values["mtf_bullish"] = (
                    context["regime"] == "trending"
                    and context["direction"] == "bullish"
                )
        else:
            values["mtf_bullish"] = None

        totals["observations"] += 1
        totals["directional_long"] += int(metrics.get("direction") == "bullish")
        totals["qualified_long"] += int(metrics.get("signal") == "LONG")
        for name, value in values.items():
            if value is None:
                counts[name]["not_applicable"] += 1
            else:
                counts[name]["pass" if value else "fail"] += 1
        for left, right in combinations(gates[:4], 2):
            a, b = values[left], values[right]
            key = f"{left}&{right}"
            if a and b:
                bucket = "both_pass"
            elif a and b is False:
                bucket = "left_pass_right_fail"
            elif a is False and b:
                bucket = "left_fail_right_pass"
            elif a is False and b is False:
                bucket = "both_fail"
            else:
                continue
            pairwise[key][bucket] += 1

    return {
        "scope": "all closed-candle decisions in the scored window; not only rejected signals",
        "gates": counts,
        "pairwise_intersections": pairwise,
        "totals": totals,
        "notes": {
            "mtf_bullish": "Only applicable to 15m with 4h confirmation enabled; warm-up rows are not applicable.",
            "ema_gates": "Raw gate truth is counted even when a research override disables that gate.",
            "qualified_long": "Final LONG signal after technical, volume, ATR and net-RR eligibility checks; MTF is applied by the backtest stage.",
        },
    }
