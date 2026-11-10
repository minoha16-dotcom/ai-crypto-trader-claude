"""Conservative single-position OHLCV backtest using the production indicator function.

Signals are evaluated on closed candles and entered at the next candle open.
When a candle touches both stop and target, the stop is assumed to fill first.
This is a research baseline, not an exchange execution simulator.
"""
from .config import settings
from .indicators import analyze


def run_backtest(candles, starting_balance=10000.0):
    if len(candles) < 52:
        raise ValueError("At least 52 candles are required for next-open backtesting")
    trades = []
    equity = float(starting_balance)
    peak = equity
    max_drawdown = 0.0
    fee_rate = float(settings.taker_fee_rate)
    slippage = float(settings.slippage_bps) / 10000.0
    i = 49
    while i < len(candles) - 1:
        metrics = analyze(candles[: i + 1])
        side = metrics["signal"]
        if side not in ("LONG", "SHORT"):
            i += 1
            continue
        entry_bar = candles[i + 1]
        entry = float(entry_bar[1]) * (1 + slippage if side == "LONG" else 1 - slippage)
        distance = max(float(metrics["atr14"]) * 2, float(metrics["price"]) * 0.005)
        stop = entry - distance if side == "LONG" else entry + distance
        target = entry + 2 * distance if side == "LONG" else entry - 2 * distance
        qty = (equity * float(settings.risk_per_trade_pct) / 100) / (
            distance + (entry + (stop if side == "LONG" else target)) * (fee_rate + slippage)
        )
        direction = 1 if side == "LONG" else -1
        exit_price = None
        exit_reason = None
        j = i + 1
        while j < len(candles):
            high, low = float(candles[j][2]), float(candles[j][3])
            stop_hit = low <= stop if side == "LONG" else high >= stop
            target_hit = high >= target if side == "LONG" else low <= target
            if stop_hit:
                exit_price, exit_reason = stop, "STOP"
                break
            if target_hit:
                exit_price, exit_reason = target, "TARGET"
                break
            j += 1
        if exit_price is None:
            break
        exit_price *= (1 - slippage if side == "LONG" else 1 + slippage)
        gross = (exit_price - entry) * qty * direction
        costs = (entry + exit_price) * qty * fee_rate
        pnl = gross - costs
        initial_risk = distance * qty + (entry + stop) * qty * (fee_rate + slippage)
        r_multiple = pnl / initial_risk if initial_risk else 0.0
        equity += pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100 if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        trades.append({
            "side": side, "entry": entry, "stop": stop, "target": target,
            "exit": exit_price, "exit_reason": exit_reason, "quantity": qty,
            "gross_pnl": gross, "costs": costs, "pnl": pnl, "r_multiple": r_multiple,
        })
        i = j + 1
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    return {
        "trades": trades,
        "trade_count": len(trades),
        "starting_balance": starting_balance,
        "ending_balance": equity,
        "net_pnl": equity - starting_balance,
        "expectancy_r": sum(t["r_multiple"] for t in trades) / len(trades) if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (None if gross_profit == 0 else float("inf")),
        "max_drawdown_pct": max_drawdown,
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "assumptions": {
            "entry": "next candle open",
            "same_candle_stop_and_target": "stop first",
            "fee_rate_per_side": fee_rate,
            "slippage_bps_per_side": float(settings.slippage_bps),
            "funding": "not modeled",
            "position_mode": "one position at a time",
        },
    }
