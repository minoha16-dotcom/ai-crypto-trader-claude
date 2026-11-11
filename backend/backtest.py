"""Conservative OHLCV backtest with Spot and exchange-aware Futures sizing.

Signals are evaluated on closed candles and entered at the next candle open.
When one candle touches both stop and target, stop is assumed to fill first.
This is a research simulation, not a full exchange execution or margin model.
"""
import math

from .config import settings
from .contracts import (
    contract_gross_pnl,
    contract_notional,
    contract_risk_amount,
    normalize_amount,
)
from .indicators import analyze


def run_backtest(candles, starting_balance=10000.0, instrument=None):
    if len(candles) < 52:
        raise ValueError("At least 52 candles are required for next-open backtesting")
    starting_balance = float(starting_balance)
    if not math.isfinite(starting_balance) or starting_balance <= 0:
        raise ValueError("starting_balance must be finite and positive")

    spec = instrument or {"contract_size": 1.0, "contract_type": "spot"}
    contract_type = spec.get("contract_type", "spot")
    contract_size = float(spec.get("contract_size", 1.0))
    if contract_type not in ("spot", "linear", "inverse"):
        raise ValueError("Unsupported backtest instrument contract type")
    if not math.isfinite(contract_size) or contract_size <= 0:
        raise ValueError("Backtest contract size must be finite and positive")
    if contract_type == "spot" and instrument and instrument.get("market_type") == "futures":
        raise ValueError("Futures backtests require linear or inverse contract metadata")

    fee_rate = float(settings.taker_fee_rate)
    slippage = float(settings.slippage_bps) / 10000.0
    risk_pct = float(settings.risk_per_trade_pct)
    max_notional_pct = float(settings.max_position_notional_pct)
    if any(not math.isfinite(v) or v < 0 for v in (fee_rate, slippage, risk_pct, max_notional_pct)):
        raise ValueError("Backtest costs and risk settings must be finite and non-negative")
    if slippage >= 1:
        raise ValueError("Backtest slippage must be less than 100%")

    trades = []
    skipped_signals = []
    equity = starting_balance
    peak = equity
    max_drawdown = 0.0
    i = 49

    while i < len(candles) - 1:
        metrics = analyze(candles[: i + 1])
        side = metrics["signal"]
        if side not in ("LONG", "SHORT"):
            i += 1
            continue

        raw_entry = float(candles[i + 1][1])
        reference_price = float(metrics["price"])
        atr_value = float(metrics["atr14"])
        if not all(math.isfinite(v) and v > 0 for v in (raw_entry, reference_price, atr_value)):
            skipped_signals.append({"index": i, "reason": "Invalid entry or indicator price"})
            i += 1
            continue

        direction = 1 if side == "LONG" else -1
        entry = raw_entry * (1 + slippage if side == "LONG" else 1 - slippage)
        distance = max(atr_value * 2, reference_price * 0.005)
        stop = raw_entry - distance if side == "LONG" else raw_entry + distance
        target = raw_entry + 2 * distance if side == "LONG" else raw_entry - 2 * distance
        stop_fill = stop * (1 - slippage if side == "LONG" else 1 + slippage)

        per_contract_risk = contract_risk_amount(
            entry, stop_fill, 1.0, contract_size, contract_type
        ) + (
            contract_notional(entry, 1.0, contract_size, contract_type)
            + contract_notional(stop_fill, 1.0, contract_size, contract_type)
        ) * fee_rate
        per_contract_notional = contract_notional(
            entry, 1.0, contract_size, contract_type
        )
        if per_contract_risk <= 0 or per_contract_notional <= 0:
            skipped_signals.append({"index": i, "reason": "Invalid contract risk or notional"})
            i += 1
            continue

        risk_budget = equity * risk_pct / 100.0
        notional_cap = equity * max_notional_pct / 100.0
        quantity = min(risk_budget / per_contract_risk, notional_cap / per_contract_notional)
        if contract_type != "spot":
            try:
                quantity = normalize_amount(
                    quantity,
                    spec.get("amount_precision"),
                    spec.get("precision_mode"),
                    spec.get("min_amount"),
                    spec.get("max_amount"),
                )
            except (TypeError, ValueError) as exc:
                skipped_signals.append({"index": i, "reason": str(exc)})
                i += 1
                continue
        else:
            quantity = math.nextafter(quantity, 0.0) if quantity * per_contract_risk > risk_budget else quantity

        notional = contract_notional(entry, quantity, contract_size, contract_type)
        if (
            not math.isfinite(quantity) or quantity <= 0
            or notional > notional_cap * (1 + 1e-12)
        ):
            skipped_signals.append({"index": i, "reason": "Position is outside risk or notional limits"})
            i += 1
            continue
        if spec.get("min_cost") is not None and notional < float(spec["min_cost"]):
            skipped_signals.append({"index": i, "reason": "Order notional is below exchange minimum"})
            i += 1
            continue
        if spec.get("max_cost") is not None and notional > float(spec["max_cost"]):
            skipped_signals.append({"index": i, "reason": "Order notional exceeds exchange maximum"})
            i += 1
            continue

        exit_reference = None
        exit_reason = None
        j = i + 1
        while j < len(candles):
            high, low = float(candles[j][2]), float(candles[j][3])
            stop_hit = low <= stop if side == "LONG" else high >= stop
            target_hit = high >= target if side == "LONG" else low <= target
            if stop_hit:
                exit_reference, exit_reason = stop, "STOP"
                break
            if target_hit:
                exit_reference, exit_reason = target, "TARGET"
                break
            j += 1

        if exit_reference is None:
            break

        exit_price = exit_reference * (1 - slippage if side == "LONG" else 1 + slippage)
        gross = contract_gross_pnl(
            entry, exit_price, quantity, side, contract_size, contract_type
        )
        costs = (
            contract_notional(entry, quantity, contract_size, contract_type)
            + contract_notional(exit_price, quantity, contract_size, contract_type)
        ) * fee_rate
        pnl = gross - costs
        initial_risk = contract_risk_amount(
            entry, stop_fill, quantity, contract_size, contract_type
        ) + (
            contract_notional(entry, quantity, contract_size, contract_type)
            + contract_notional(stop_fill, quantity, contract_size, contract_type)
        ) * fee_rate
        r_multiple = pnl / initial_risk if initial_risk > 0 else 0.0

        equity += pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100 if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, drawdown)
        trades.append({
            "side": side,
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit": exit_price,
            "exit_reason": exit_reason,
            "quantity": quantity,
            "contract_size": contract_size,
            "contract_type": contract_type,
            "notional": notional,
            "gross_pnl": gross,
            "costs": costs,
            "pnl": pnl,
            "r_multiple": r_multiple,
        })
        i = j + 1

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    return {
        "trades": trades,
        "trade_count": len(trades),
        "skipped_signals": skipped_signals,
        "starting_balance": starting_balance,
        "ending_balance": equity,
        "net_pnl": equity - starting_balance,
        "expectancy_r": sum(trade["r_multiple"] for trade in trades) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown,
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "instrument": {
            "contract_type": contract_type,
            "contract_size": contract_size,
        },
        "assumptions": {
            "entry": "next candle open",
            "same_candle_stop_and_target": "stop first",
            "fee_rate_per_side": fee_rate,
            "slippage_bps_per_side": float(settings.slippage_bps),
            "funding": "not modeled",
            "margin_and_liquidation": "not modeled",
            "position_mode": "one position at a time",
            "position_notional_cap_pct": max_notional_pct,
            "futures_quantity": "exchange contract precision and limits applied" if contract_type != "spot" else "base-asset units",
            "profit_factor": "null when there are no losing trades",
        },
    }
