from .database import connect
from .config import settings
from .risk import risk_manager


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


def open_trade(exchange, symbol, market_type, side, entry, stop, target=None, signal_id=None):
    if side not in ("LONG", "SHORT"):
        raise ValueError("Side must be LONG or SHORT")
    if market_type not in ("spot", "futures"):
        raise ValueError("market_type must be spot or futures")
    if market_type == "spot" and side == "SHORT":
        raise ValueError("SHORT paper positions are not supported for spot markets; select futures.")
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        raise ValueError("Stop must be below entry for LONG and above entry for SHORT")
    if target is not None and ((side == "LONG" and target <= entry) or (side == "SHORT" and target >= entry)):
        raise ValueError("Target must be above entry for LONG and below entry for SHORT")

    with connect() as db:
        # SQLite obtains a write reservation; PostgreSQL takes a transaction-scoped
        # advisory lock in the adapter so concurrent requests share one risk budget.
        db.execute("BEGIN IMMEDIATE")
        realized = db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE status='CLOSED'"
        ).fetchone()["total"]
        equity = settings.paper_starting_balance + float(realized or 0)
        opened = db.execute(
            "SELECT quantity, entry, stop FROM trades WHERE status='OPEN'"
        ).fetchall()
        open_risk = sum(
            abs(float(row["entry"]) - float(row["stop"])) * float(row["quantity"])
            for row in opened
        )
        today_pnl = db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
            "WHERE status='CLOSED' AND date(closed_at)=date('now')"
        ).fetchone()["total"]
        today_start = db.execute(
            "SELECT starting_equity FROM daily_equity_snapshots WHERE trading_date=date('now')"
        ).fetchone()
        if today_start is None:
            db.execute(
                "INSERT INTO daily_equity_snapshots(trading_date, starting_equity) "
                "VALUES(date('now'), ?)", (equity - float(today_pnl or 0),)
            )
            daily_start_equity = equity - float(today_pnl or 0)
        else:
            daily_start_equity = float(today_start["starting_equity"])

        decision = risk_manager.size(
            equity, float(entry), float(stop), open_risk=open_risk,
            daily_pnl=float(today_pnl or 0), daily_start_equity=daily_start_equity,
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
        "risk_amount": decision.risk_amount, "status": "OPEN",
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
        direction = 1 if row["side"] == "LONG" else -1
        pnl = (exit_price - row["entry"]) * row["quantity"] * direction
        db.execute(
            "UPDATE trades SET status='CLOSED',closed_at=CURRENT_TIMESTAMP,exit_price=?,pnl=? "
            "WHERE id=? AND status='OPEN'", (exit_price, pnl, trade_id)
        )
    return {"id": trade_id, "exit_price": exit_price, "pnl": pnl, "status": "CLOSED"}


def journal():
    with connect() as db:
        trades = [dict(row) for row in db.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()]
        signals = [dict(row) for row in db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 300").fetchall()]
    return {"trades": trades, "signals": signals}
