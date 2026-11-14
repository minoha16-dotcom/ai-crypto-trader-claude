from .database import connect
from .config import settings
from .risk import risk_manager
import time

from .metrics import position_risk, position_pnl, trading_cost_rate
from .contracts import contract_notional
from .futures import (
    due_funding,
    initial_margin,
    liquidation_price,
    liquidation_triggered,
    validate_futures_parameters,
)



def account():
    with connect() as db:
        rows = db.execute("SELECT * FROM trades").fetchall()
    realized = sum(float(row["pnl"] or 0) for row in rows if row["status"] == "CLOSED")
    opened = [dict(row) for row in rows if row["status"] == "OPEN"]
    return {
        "starting_balance": settings.paper_starting_balance,
        "realized_pnl": realized,
        "unrealized_pnl": None,
        "equity": settings.paper_starting_balance + realized,
        "open_trades": opened,
        "unrealized_pnl_note": "Not marked to market: paper positions require a live price feed.",
    }


def open_trade(
    exchange, symbol, market_type, side, entry, stop, target=None,
    signal_id=None, unrealized_pnl=0.0, prices_complete=True, instrument=None,
    leverage=1.0, funding_rate=None, funding_interval_hours=None,
    maintenance_margin_rate=None, liquidation_fee_rate=None,
):
    if side not in ("LONG", "SHORT"):
        raise ValueError("Side must be LONG or SHORT")
    if market_type not in ("spot", "futures"):
        raise ValueError("market_type must be spot or futures")
    if market_type == "spot" and side == "SHORT":
        raise ValueError("SHORT paper positions are not supported for spot markets; select futures.")
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        raise ValueError("Stop must be below entry for LONG and above entry for SHORT")
    if target is None:
        reward = abs(float(entry) - float(stop)) * float(settings.reward_risk_ratio)
        target = float(entry) + reward if side == "LONG" else float(entry) - reward
    if (side == "LONG" and target <= entry) or (side == "SHORT" and target >= entry):
        raise ValueError("Target must be above entry for LONG and below entry for SHORT")
    if not prices_complete:
        raise ValueError("Cannot open a trade: live price unavailable for at least one open position.")
    if market_type == "futures" and not instrument:
        raise ValueError("Futures paper positions require validated exchange contract metadata.")
    # Direct engine callers without venue metadata are kept deterministic for
    # local research; API-created trades always pass real CCXT market limits.
    spec = instrument or {
        "contract_size": 1.0, "contract_type": "spot",
        "amount_precision": 12, "precision_mode": 2,
    }
    if market_type == "futures" and spec.get("contract_type") not in ("linear", "inverse"):
        raise ValueError("Futures instrument must be a validated linear or inverse contract.")
    if market_type == "spot" and spec.get("contract_type") != "spot":
        raise ValueError("Spot instrument metadata is inconsistent with market type.")

    if market_type == "futures":
        futures = validate_futures_parameters(
            leverage=leverage,
            funding_rate=(
                settings.paper_futures_default_funding_rate
                if funding_rate is None else funding_rate
            ),
            funding_interval_hours=(
                settings.paper_futures_funding_interval_hours
                if funding_interval_hours is None else funding_interval_hours
            ),
            maintenance_margin_rate=(
                settings.paper_futures_maintenance_margin_rate
                if maintenance_margin_rate is None else maintenance_margin_rate
            ),
            liquidation_fee_rate=(
                settings.paper_futures_liquidation_fee_rate
                if liquidation_fee_rate is None else liquidation_fee_rate
            ),
            max_leverage=settings.max_futures_leverage,
        )
        leverage = futures["leverage"]
        funding_rate = futures["funding_rate"]
        funding_interval_hours = futures["funding_interval_hours"]
        maintenance_margin_rate = futures["maintenance_margin_rate"]
        liquidation_fee_rate = futures["liquidation_fee_rate"]
    else:
        leverage = 1.0
        funding_rate = 0.0
        funding_interval_hours = settings.paper_futures_funding_interval_hours
        maintenance_margin_rate = settings.paper_futures_maintenance_margin_rate
        liquidation_fee_rate = settings.paper_futures_liquidation_fee_rate

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if signal_id is not None:
            source_signal = db.execute(
                "SELECT exchange, symbol, market_type, signal FROM signals WHERE id=?",
                (signal_id,),
            ).fetchone()
            if not source_signal:
                raise ValueError("The selected scanner setup no longer exists")
            if (
                str(source_signal["exchange"]).lower() != str(exchange).lower()
                or str(source_signal["symbol"]).upper() != str(symbol).upper()
                or source_signal["market_type"] != market_type
                or source_signal["signal"] != side
            ):
                raise ValueError("Selected scanner setup does not match this paper trade")
        opened = db.execute(
            "SELECT exchange, symbol, market_type, quantity, entry, stop, contract_size, contract_type, initial_margin "
            "FROM trades WHERE status='OPEN'"
        ).fetchall()
        if len(opened) >= settings.max_open_trades:
            raise ValueError(
                f"Maximum open paper positions reached ({settings.max_open_trades})."
            )
        duplicate = any(
            str(row["exchange"]).lower() == str(exchange).lower()
            and str(row["symbol"]).upper() == str(symbol).upper()
            and row["market_type"] == market_type
            for row in opened
        )
        if duplicate:
            raise ValueError("An open position already exists for this exchange, symbol and market.")
        realized = db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE status='CLOSED'"
        ).fetchone()["total"]
        equity = settings.paper_starting_balance + float(realized or 0)
        open_risk = sum(position_risk(
            row["entry"], row["stop"], row["quantity"],
            row["contract_size"], row["contract_type"],
        ) for row in opened)
        margin_used = sum(float(row.get("initial_margin") or 0) for row in opened)
        today_pnl = db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
            "WHERE status='CLOSED' AND date(closed_at)=date('now')"
        ).fetchone()["total"]
        today_start = db.execute(
            "SELECT starting_equity FROM daily_equity_snapshots WHERE trading_date=date('now')"
        ).fetchone()
        if today_start is None:
            daily_start_equity = equity - float(today_pnl or 0)
            db.execute(
                "INSERT INTO daily_equity_snapshots(trading_date, starting_equity) "
                "VALUES(date('now'), ?)", (daily_start_equity,)
            )
        else:
            daily_start_equity = float(today_start["starting_equity"])
        combined_daily_pnl = float(today_pnl or 0) + float(unrealized_pnl or 0)
        decision = risk_manager.size(
            equity, float(entry), float(stop), open_risk=open_risk,
            daily_pnl=combined_daily_pnl, daily_start_equity=daily_start_equity,
            instrument=spec, leverage=leverage, margin_used=margin_used,
        )
        if not decision.approved:
            raise ValueError(decision.reason)
        notional = decision.notional
        margin = initial_margin(notional, leverage) if market_type == "futures" else 0.0
        if market_type == "futures":
            estimated_liquidation_price = liquidation_price(
                entry, decision.quantity, float(spec["contract_size"]),
                spec["contract_type"], side, leverage,
                maintenance_margin_rate, trading_cost_rate(),
                liquidation_fee_rate, 0.0,
            )
            next_funding_at = time.time() + float(funding_interval_hours) * 3600.0
        else:
            estimated_liquidation_price = None
            next_funding_at = None
        cursor = db.execute(
            "INSERT INTO trades("
            "exchange,symbol,market_type,side,quantity,entry,stop,target,signal_id,"
            "contract_size,contract_type,leverage,initial_margin,maintenance_margin_rate,"
            "liquidation_fee_rate,liquidation_price,funding_rate,funding_interval_hours,"
            "funding_paid,funding_events,next_funding_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (
                exchange, symbol, market_type, side, decision.quantity, entry, stop,
                target, signal_id, float(spec["contract_size"]), spec["contract_type"],
                leverage, margin, maintenance_margin_rate, liquidation_fee_rate,
                estimated_liquidation_price, funding_rate, funding_interval_hours,
                0.0, 0, next_funding_at,
            ),
        )
        trade_id = cursor.fetchone()["id"]
    return {
        "id": trade_id, "quantity": decision.quantity, "contracts": decision.quantity if market_type == "futures" else None,
        "contract_size": float(spec["contract_size"]), "contract_type": spec["contract_type"],
        "notional": decision.notional, "risk_amount": decision.risk_amount,
        "target": target,
        "reward_risk_ratio": abs(float(target) - float(entry)) / abs(float(entry) - float(stop)),
        "default_reward_risk_ratio": float(settings.reward_risk_ratio),
        "risk_pct_actual": decision.risk_amount / equity * 100,
        "sizing_reason": decision.sizing_reason,
        "leverage": leverage,
        "initial_margin": margin,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_fee_rate": liquidation_fee_rate,
        "liquidation_price": estimated_liquidation_price,
        "funding_rate": funding_rate,
        "funding_interval_hours": funding_interval_hours,
        "funding_paid": 0.0,
        "funding_events": 0,
        "next_funding_at": next_funding_at,
        "fees_estimated": position_pnl(
            entry, decision.quantity, side, stop,
            spec["contract_size"], spec["contract_type"],
        )["estimated_costs"],
        "status": "OPEN",
    }


def apply_due_funding(trade_id, mark_price, now=None):
    """Accrue due paper funding intervals once, using the current observed mark.

    If a monitor run is late, multiple missed intervals are accrued at the
    currently observed notional. Historical per-interval mark prices are not
    available to this live paper ledger and this approximation is disclosed.
    """
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise ValueError("Funding accrual time must be finite")
    mark = float(mark_price)
    if not math.isfinite(mark) or mark <= 0:
        raise ValueError("Funding mark price must be finite and positive")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)
        ).fetchone()
        if not row:
            raise ValueError("Open paper trade not found")
        result = dict(row)
        if result["market_type"] != "futures" or result.get("next_funding_at") is None:
            return result
        payment, events, next_at = due_funding(
            mark, result["quantity"], result["contract_size"],
            result["contract_type"], result["side"], result["funding_rate"],
            result["next_funding_at"], current, result["funding_interval_hours"],
        )
        if events:
            result["funding_paid"] = float(result.get("funding_paid") or 0) + payment
            result["funding_events"] = int(result.get("funding_events") or 0) + events
            result["next_funding_at"] = next_at
            db.execute(
                "UPDATE trades SET funding_paid=?,funding_events=?,next_funding_at=? "
                "WHERE id=? AND status='OPEN'",
                (
                    result["funding_paid"], result["funding_events"],
                    next_at, trade_id,
                ),
            )
        return result


def close_trade(trade_id, exit_price, exit_reason="MANUAL", observed_at=None):
    exit_price = float(exit_price)
    if not math.isfinite(exit_price) or exit_price <= 0:
        raise ValueError("Exit price must be finite and positive")
    # Apply any due funding before final P&L is calculated.
    try:
        apply_due_funding(trade_id, exit_price, now=observed_at)
    except ValueError as exc:
        if "not found" in str(exc):
            raise
        raise

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)
        ).fetchone()
        if not row:
            raise ValueError("Open paper trade not found")

        funding_paid = float(row["funding_paid"] or 0)
        extra_exit_fee = 0.0
        if row["market_type"] == "futures":
            hit_liquidation = (
                row["liquidation_price"] is not None
                and liquidation_triggered(
                    row["side"], exit_price, row["liquidation_price"]
                )
            )
            if hit_liquidation:
                exit_reason = "LIQUIDATION"
                extra_exit_fee = contract_notional(
                    exit_price, row["quantity"], row["contract_size"],
                    row["contract_type"],
                ) * float(row["liquidation_fee_rate"] or 0)

        marked = position_pnl(
            row["entry"], row["quantity"], row["side"], exit_price,
            row["contract_size"], row["contract_type"],
            funding_paid=funding_paid,
            additional_exit_fee=extra_exit_fee,
        )
        pnl = marked["unrealized_pnl"]
        db.execute(
            "UPDATE trades SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,"
            "exit_price=?,pnl=?,exit_reason=? WHERE id=? AND status='OPEN'",
            (exit_price, pnl, exit_reason, trade_id),
        )
    initial_risk = position_risk(
        row["entry"], row["stop"], row["quantity"],
        row["contract_size"], row["contract_type"],
    )
    return {
        "id": trade_id,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "gross_pnl": marked["gross_pnl"],
        "costs": marked["estimated_costs"],
        "funding_paid": funding_paid,
        "funding_events": int(row["funding_events"] or 0),
        "liquidation_fee": extra_exit_fee,
        "pnl": pnl,
        "r_multiple": pnl / initial_risk if initial_risk else 0.0,
        "status": "CLOSED",
    }

def journal(limit=100, offset=0):
    limit = max(1, min(int(limit), 500))
    offset = max(0, min(int(offset), 1_000_000))
    with connect() as db:
        trades = [dict(row) for row in db.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()]
        signals = [dict(row) for row in db.execute(
            "SELECT * FROM signals WHERE archived_at IS NULL ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()]
        trades_total = db.execute("SELECT COUNT(*) AS total FROM trades").fetchone()["total"]
        signals_total = db.execute(
            "SELECT COUNT(*) AS total FROM signals WHERE archived_at IS NULL"
        ).fetchone()["total"]
    for trade in trades:
        if trade.get("status") == "CLOSED":
            initial_risk = position_risk(
                trade["entry"], trade["stop"], trade["quantity"],
                trade.get("contract_size", 1.0), trade.get("contract_type", "spot"),
            )
            trade["r_multiple"] = float(trade["pnl"] or 0) / initial_risk if initial_risk else 0.0
        else:
            trade["r_multiple"] = None
    return {"trades": trades, "signals": signals, "pagination": {
        "limit": limit, "offset": offset,
        "trades_total": trades_total, "signals_total": signals_total,
        "trades_has_more": offset + len(trades) < trades_total,
        "signals_has_more": offset + len(signals) < signals_total,
    }}

