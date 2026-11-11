from .database import connect
from .config import settings
from .risk import risk_manager
from .metrics import position_risk, position_pnl, trading_cost_rate

MAX_OPEN_TRADES = 3
REWARD_RISK_RATIO = 2.0


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


def open_trade(exchange, symbol, market_type, side, entry, stop, target=None,
               signal_id=None, unrealized_pnl=0.0, prices_complete=True):
    if side not in ("LONG", "SHORT"):
        raise ValueError("Side must be LONG or SHORT")
    if market_type not in ("spot", "futures"):
        raise ValueError("market_type must be spot or futures")
    if market_type == "spot" and side == "SHORT":
        raise ValueError("SHORT paper positions are not supported for spot markets; select futures.")
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        raise ValueError("Stop must be below entry for LONG and above entry for SHORT")
    if target is None:
        reward = abs(float(entry) - float(stop)) * REWARD_RISK_RATIO
        target = float(entry) + reward if side == "LONG" else float(entry) - reward
    if (side == "LONG" and target <= entry) or (side == "SHORT" and target >= entry):
        raise ValueError("Target must be above entry for LONG and below entry for SHORT")
    if not prices_complete:
        raise ValueError("Cannot open a trade: live price unavailable for at least one open position.")

    with connect() as db:
        # Serializes all risk checks and inserts across concurrent requests.
        db.execute("BEGIN IMMEDIATE")
        opened = db.execute(
            "SELECT exchange, symbol, market_type, quantity, entry, stop FROM trades WHERE status='OPEN'"
        ).fetchall()
        if len(opened) >= MAX_OPEN_TRADES:
            raise ValueError(f"Maximum open paper positions reached ({MAX_OPEN_TRADES}).")
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
        open_risk = sum(position_risk(row["entry"], row["stop"], row["quantity"]) for row in opened)
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
        )
        if not decision.approved:
            raise ValueError(decision.reason)
        cursor = db.execute(
            "INSERT INTO trades(exchange,symbol,market_type,side,quantity,entry,stop,target,signal_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (exchange, symbol, market_type, side, decision.quantity, entry, stop, target, signal_id),
        )
        trade_id = cursor.fetchone()["id"]
    return {
        "id": trade_id, "quantity": decision.quantity, "notional": decision.notional,
        "risk_amount": decision.risk_amount, "target": target, "reward_risk_ratio": REWARD_RISK_RATIO,
        "risk_pct_actual": decision.risk_amount / equity * 100,
        "sizing_reason": decision.sizing_reason,
        "fees_estimated": decision.quantity * (float(entry) + float(stop)) * (float(settings.taker_fee_rate) + float(settings.slippage_bps) / 10000),
        "status": "OPEN",
    }


def close_trade(trade_id, exit_price):
    if exit_price <= 0:
        raise ValueError("Exit price must be positive")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)
        ).fetchone()
        if not row:
            raise ValueError("Open paper trade not found")
        marked = position_pnl(row["entry"], row["quantity"], row["side"], exit_price)
        gross_pnl = marked["gross_pnl"]
        pnl = marked["unrealized_pnl"]
        costs = marked["estimated_costs"]
        db.execute(
            "UPDATE trades SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,exit_price=?,pnl=? "
            "WHERE id=? AND status='OPEN'", (exit_price, pnl, trade_id)
        )
    initial_risk = position_risk(row["entry"], row["stop"], row["quantity"])
    return {"id": trade_id, "exit_price": exit_price, "gross_pnl": gross_pnl,
            "costs": costs, "pnl": pnl,
            "r_multiple": pnl / initial_risk if initial_risk else 0.0, "status": "CLOSED"}


def journal():
    with connect() as db:
        trades = [dict(row) for row in db.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()]
        signals = [dict(row) for row in db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 300").fetchall()]
    for trade in trades:
        if trade.get("status") == "CLOSED":
            initial_risk = position_risk(trade["entry"], trade["stop"], trade["quantity"])
            trade["r_multiple"] = float(trade["pnl"] or 0) / initial_risk if initial_risk else 0.0
        else:
            trade["r_multiple"] = None
    return {"trades": trades, "signals": signals}
