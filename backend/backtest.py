"""Conservative OHLCV backtest with gap-aware fills and mark-to-market equity.

Signals are evaluated on closed candles and entered at the next candle open.
A stop-market order is assumed to fill at an adverse gap open when the market
opens beyond the stop. If stop and target both trade inside one candle, stop
is assumed first. This remains a bar-based simulation, not exchange execution.
"""
import math
from datetime import datetime, timezone

from .config import settings
from .contracts import (
    contract_gross_pnl,
    contract_notional,
    contract_risk_amount,
    normalize_amount,
)
from .indicators import analyze
from .metrics import validate_trading_configuration


def _costed_pnl(entry, mark, quantity, side, contract_size, contract_type, fee_rate):
    gross = contract_gross_pnl(
        entry, mark, quantity, side, contract_size, contract_type
    )
    costs = (
        contract_notional(entry, quantity, contract_size, contract_type)
        + contract_notional(mark, quantity, contract_size, contract_type)
    ) * fee_rate
    return gross - costs, gross, costs


def _drawdown(peak, value):
    return (peak - value) / peak * 100.0 if peak > 0 else 0.0


def _utc_day(timestamp):
    """Return the UTC date for a CCXT millisecond timestamp."""
    value = float(timestamp)
    seconds = value / 1000.0 if abs(value) >= 100_000_000_000 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()


def _validate_candles(candles):
    for index, candle in enumerate(candles):
        if len(candle) < 6:
            raise ValueError(f"Candle {index} must contain timestamp and OHLCV values")
        timestamp, open_price, high, low, close, volume = map(float, candle[:6])
        if not all(math.isfinite(value) for value in (
            timestamp, open_price, high, low, close, volume
        )):
            raise ValueError(f"Candle {index} contains a non-finite value")
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError(f"Candle {index} contains an invalid OHLCV value")
        if high < max(open_price, close, low) or low > min(open_price, close):
            raise ValueError(f"Candle {index} has inconsistent high/low values")


def run_backtest(candles, starting_balance=10000.0, instrument=None):
    if len(candles) < 52:
        raise ValueError("At least 52 candles are required for next-open backtesting")
    _validate_candles(candles)
    starting_balance = float(starting_balance)
    if not math.isfinite(starting_balance) or starting_balance <= 0:
        raise ValueError("starting_balance must be finite and positive")

    spec = instrument or {
        "contract_size": 1.0, "contract_type": "spot",
        "amount_precision": 12, "precision_mode": 2,
    }
    contract_type = spec.get("contract_type", "spot")
    contract_size = float(spec.get("contract_size", 1.0))
    if contract_type not in ("spot", "linear", "inverse"):
        raise ValueError("Unsupported backtest instrument contract type")
    if not math.isfinite(contract_size) or contract_size <= 0:
        raise ValueError("Backtest contract size must be finite and positive")
    if contract_type == "spot" and instrument and instrument.get("market_type") == "futures":
        raise ValueError("Futures backtests require linear or inverse contract metadata")

    configuration = validate_trading_configuration()
    fee_rate = configuration["fee_rate"]
    slippage = configuration["slippage"]
    risk_pct = configuration["risk_pct"]
    max_notional_pct = configuration["max_notional_pct"]
    max_open_risk_pct = configuration["max_open_risk_pct"]
    max_daily_loss_pct = configuration["max_daily_loss_pct"]

    # A daily starting-equity snapshot and realized P&L ledger mirror the live
    # risk engine's UTC-date accounting. One backtest position is open at once.
    daily_start_equity = {}
    daily_realized_pnl = {}

    trades = []
    skipped_signals = []
    equity_curve = []
    equity = starting_balance  # realized balance; open P&L is not booked
    peak_equity = equity
    max_drawdown = 0.0
    open_position = None
    i = 49

    while i < len(candles) - 1:
        metrics = analyze(candles[: i + 1])
        side = metrics["signal"]
        if side not in ("LONG", "SHORT"):
            i += 1
            continue

        entry_day = _utc_day(candles[i + 1][0])
        daily_start_equity.setdefault(entry_day, equity)
        daily_realized_pnl.setdefault(entry_day, 0.0)
        daily_loss_limit = daily_start_equity[entry_day] * max_daily_loss_pct / 100.0
        if daily_realized_pnl[entry_day] <= -daily_loss_limit:
            skipped_signals.append({
                "index": i,
                "reason": "DAILY_LOSS_LIMIT",
                "daily_realized_pnl": daily_realized_pnl[entry_day],
                "daily_loss_limit": daily_loss_limit,
            })
            i += 1
            continue

        raw_entry = float(candles[i + 1][1])
        reference_price = float(metrics["price"])
        atr_value = float(metrics["atr14"])
        if not all(math.isfinite(value) and value > 0 for value in
                   (raw_entry, reference_price, atr_value)):
            skipped_signals.append({
                "index": i, "reason": "Invalid entry or indicator price"
            })
            i += 1
            continue

        entry = raw_entry * (1 + slippage if side == "LONG" else 1 - slippage)
        distance = max(atr_value * 2, reference_price * 0.005)
        stop = raw_entry - distance if side == "LONG" else raw_entry + distance
        target = raw_entry + 2 * distance if side == "LONG" else raw_entry - 2 * distance
        if stop <= 0:
            skipped_signals.append({"index": i, "reason": "Calculated stop is not positive"})
            i += 1
            continue
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
            skipped_signals.append({
                "index": i, "reason": "Invalid contract risk or notional"
            })
            i += 1
            continue

        risk_budget = equity * risk_pct / 100.0
        max_open_risk_budget = equity * max_open_risk_pct / 100.0
        notional_cap = equity * max_notional_pct / 100.0
        # One-at-a-time backtests have no pre-existing open risk. Applying the
        # aggregate cap explicitly keeps sizing aligned with RiskManager.
        quantity = min(
            risk_budget / per_contract_risk,
            max_open_risk_budget / per_contract_risk,
            notional_cap / per_contract_notional,
        )
        # Cap to the venue's maximum amount before precision rounding, matching
        # the live RiskManager and avoiding a valid capped order being skipped.
        max_amount = spec.get("max_amount")
        if max_amount is not None:
            max_amount = float(max_amount)
            if not math.isfinite(max_amount) or max_amount < 0:
                skipped_signals.append({
                    "index": i, "reason": "Invalid exchange maximum amount"
                })
                i += 1
                continue
            if max_amount > 0:
                quantity = min(quantity, max_amount)
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

        notional = contract_notional(entry, quantity, contract_size, contract_type)
        if (
            not math.isfinite(quantity) or quantity <= 0
            or notional > notional_cap * (1 + 1e-12)
        ):
            skipped_signals.append({
                "index": i, "reason": "Position is outside risk or notional limits"
            })
            i += 1
            continue
        if spec.get("min_cost") is not None and float(spec["min_cost"]) > 0 and notional < float(spec["min_cost"]):
            skipped_signals.append({
                "index": i, "reason": "Order notional is below exchange minimum"
            })
            i += 1
            continue
        if spec.get("max_cost") is not None and float(spec["max_cost"]) > 0 and notional > float(spec["max_cost"]):
            skipped_signals.append({
                "index": i, "reason": "Order notional exceeds exchange maximum"
            })
            i += 1
            continue

        position = {
            "side": side,
            "entry": entry,
            "stop": stop,
            "target": target,
            "quantity": quantity,
            "contract_size": contract_size,
            "contract_type": contract_type,
            "notional": notional,
            "entry_index": i + 1,
            "entry_timestamp": candles[i + 1][0],
        }
        exit_reference = None
        exit_reason = None
        exit_index = None
        j = i + 1

        while j < len(candles):
            timestamp, candle_open, high, low, close = map(float, candles[j][:5])
            bar_day = _utc_day(timestamp)
            # If a position crosses UTC midnight, seed that day's baseline
            # before its eventual realized exit is attributed to that day.
            daily_start_equity.setdefault(bar_day, equity)
            daily_realized_pnl.setdefault(bar_day, 0.0)

            # First honor gaps at the candle open. Stop-market exits fill at the
            # worse opening price; favorable target gaps fill at the opening price.
            if side == "LONG" and candle_open <= stop:
                exit_reference, exit_reason = candle_open, "STOP_GAP"
            elif side == "SHORT" and candle_open >= stop:
                exit_reference, exit_reason = candle_open, "STOP_GAP"
            elif side == "LONG" and candle_open >= target:
                exit_reference, exit_reason = candle_open, "TARGET_GAP"
            elif side == "SHORT" and candle_open <= target:
                exit_reference, exit_reason = candle_open, "TARGET_GAP"
            else:
                stop_hit = low <= stop if side == "LONG" else high >= stop
                target_hit = high >= target if side == "LONG" else low <= target
                # OHLC cannot reveal intrabar ordering; remain conservative.
                if stop_hit:
                    exit_reference, exit_reason = stop, "STOP"
                elif target_hit:
                    exit_reference, exit_reason = target, "TARGET"

            if exit_reference is not None:
                exit_index = j
                break

            # Mark-to-market drawdown uses adverse intrabar extremes, while the
            # reported unrealized P&L uses the candle close.
            favorable_mark = high if side == "LONG" else low
            adverse_mark = low if side == "LONG" else high
            favorable_price = favorable_mark * (
                1 - slippage if side == "LONG" else 1 + slippage
            )
            adverse_price = adverse_mark * (
                1 - slippage if side == "LONG" else 1 + slippage
            )
            close_price = close * (
                1 - slippage if side == "LONG" else 1 + slippage
            )
            favorable_pnl = _costed_pnl(
                entry, favorable_price, quantity, side,
                contract_size, contract_type, fee_rate
            )[0]
            adverse_pnl = _costed_pnl(
                entry, adverse_price, quantity, side,
                contract_size, contract_type, fee_rate
            )[0]
            close_pnl = _costed_pnl(
                entry, close_price, quantity, side,
                contract_size, contract_type, fee_rate
            )[0]
            peak_equity = max(peak_equity, equity + favorable_pnl)
            max_drawdown = max(
                max_drawdown,
                _drawdown(peak_equity, equity + adverse_pnl),
                _drawdown(peak_equity, equity + close_pnl),
            )
            equity_curve.append({
                "index": j,
                "timestamp": candles[j][0],
                "balance": equity,
                "equity": equity + close_pnl,
                "mark_price": close,
                "drawdown_pct": _drawdown(peak_equity, equity + close_pnl),
            })
            j += 1

        if exit_reference is None:
            final_close = float(candles[-1][4])
            mark = final_close * (
                1 - slippage if side == "LONG" else 1 + slippage
            )
            unrealized, gross_unrealized, unrealized_costs = _costed_pnl(
                entry, mark, quantity, side, contract_size, contract_type, fee_rate
            )
            open_position = {
                **position,
                "status": "OPEN",
                "mark_price": final_close,
                "gross_unrealized_pnl": gross_unrealized,
                "estimated_costs": unrealized_costs,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (
                    unrealized / notional * 100.0 if notional else 0.0
                ),
            }
            final_equity = equity + unrealized
            max_drawdown = max(max_drawdown, _drawdown(peak_equity, final_equity))
            break

        exit_price = exit_reference * (
            1 - slippage if side == "LONG" else 1 + slippage
        )
        pnl, gross, costs = _costed_pnl(
            entry, exit_price, quantity, side, contract_size, contract_type, fee_rate
        )
        initial_risk = contract_risk_amount(
            entry, stop_fill, quantity, contract_size, contract_type
        ) + (
            contract_notional(entry, quantity, contract_size, contract_type)
            + contract_notional(stop_fill, quantity, contract_size, contract_type)
        ) * fee_rate
        r_multiple = pnl / initial_risk if initial_risk > 0 else 0.0
        exit_day = _utc_day(candles[exit_index][0])
        daily_start_equity.setdefault(exit_day, equity)
        daily_realized_pnl[exit_day] = daily_realized_pnl.get(exit_day, 0.0) + pnl
        equity += pnl
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, _drawdown(peak_equity, equity))
        trades.append({
            **position,
            "exit": exit_price,
            "exit_reason": exit_reason,
            "exit_index": exit_index,
            "exit_timestamp": candles[exit_index][0],
            "gross_pnl": gross,
            "costs": costs,
            "pnl": pnl,
            "r_multiple": r_multiple,
            "status": "CLOSED",
        })
        equity_curve.append({
            "index": exit_index,
            "timestamp": candles[exit_index][0],
            "balance": equity,
            "equity": equity,
            "mark_price": exit_price,
            "drawdown_pct": _drawdown(peak_equity, equity),
        })
        i = exit_index + 1

    wins = [trade for trade in trades if trade["pnl"] > 0]
    losses = [trade for trade in trades if trade["pnl"] < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    unrealized = float(open_position["unrealized_pnl"]) if open_position else 0.0
    ending_equity = equity + unrealized

    return {
        "trades": trades,
        "trade_count": len(trades),
        "closed_trade_count": len(trades),
        "open_positions": [open_position] if open_position else [],
        "open_position": open_position,
        "skipped_signals": skipped_signals,
        "starting_balance": starting_balance,
        "ending_balance": equity,
        "ending_equity": ending_equity,
        "realized_pnl": equity - starting_balance,
        "unrealized_pnl": unrealized,
        "net_pnl": ending_equity - starting_balance,
        "expectancy_r": (
            sum(trade["r_multiple"] for trade in trades) / len(trades)
            if trades else 0.0
        ),
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_drawdown,
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "equity_curve": equity_curve,
        "instrument": {
            "contract_type": contract_type,
            "contract_size": contract_size,
        },
        "risk_limits": {
            "risk_per_trade_pct": risk_pct,
            "max_open_risk_pct": max_open_risk_pct,
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_position_notional_pct": max_notional_pct,
        },
        "assumptions": {
            "entry": "next candle open",
            "stop_gap_fill": "adverse candle open plus exit slippage",
            "target_gap_fill": "favorable candle open plus exit slippage",
            "same_candle_stop_and_target": "stop first",
            "drawdown": "mark-to-market with adverse intrabar OHLC extremes",
            "open_at_end": "reported as unrealized; not added to realized balance",
            "fee_rate_per_side": fee_rate,
            "slippage_bps_per_side": float(settings.slippage_bps),
            "funding": "not modeled",
            "margin_and_liquidation": "not modeled",
            "position_mode": "one position at a time (stricter than the live three-position limit)",
            "risk_per_trade_pct": risk_pct,
            "max_open_risk_pct": max_open_risk_pct,
            "max_daily_loss_pct": max_daily_loss_pct,
            "daily_loss_model": "blocks new entries after UTC-day realized net P&L reaches the configured loss cap",
            "position_notional_cap_pct": max_notional_pct,
            "futures_quantity": (
                "exchange contract precision and limits applied"
                if contract_type != "spot" else "base-asset units"
            ),
            "profit_factor": "null when there are no losing trades",
        },
    }
