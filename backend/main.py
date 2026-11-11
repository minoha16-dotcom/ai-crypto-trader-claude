import asyncio
import base64
import hmac
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .database import init_db, connect
from .exchanges import registry, EXCHANGE_IDS
from .scanner import scan
from .paper import open_trade, close_trade, journal, account
from .risk import risk_manager
from .backtest import run_backtest
from .position_monitor import run_position_monitor
from .metrics import position_pnl, position_risk


@asynccontextmanager
async def lifespan(app):
    init_db()
    stop_monitor = asyncio.Event()
    monitor_task = asyncio.create_task(
        run_position_monitor(stop_monitor), name="paper-position-monitor"
    )
    try:
        yield
    finally:
        stop_monitor.set()
        try:
            # Do not let a slow exchange request stall application shutdown.
            await asyncio.wait_for(monitor_task, timeout=20)
        except asyncio.TimeoutError:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        await registry.close()


app = FastAPI(
    title=settings.app_name,
    version="2.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def dashboard_auth_and_security_headers(request: Request, call_next):
    # Render health checks stay public; every dashboard/API route requires Basic Auth.
    if request.url.path != "/api/health":
        username = os.getenv("DASHBOARD_USERNAME", "")
        password = os.getenv("DASHBOARD_PASSWORD", "")
        if not username or not password:
            if os.getenv("RENDER"):
                return JSONResponse(
                    {"detail": "Dashboard credentials are not configured."},
                    status_code=503,
                )
        else:
            header = request.headers.get("authorization", "")
            supplied_user = supplied_password = ""
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
                    supplied_user, supplied_password = decoded.split(":", 1)
                except (ValueError, UnicodeDecodeError):
                    pass
            valid_user = hmac.compare_digest(supplied_user, username)
            valid_password = hmac.compare_digest(supplied_password, password)
            if not (valid_user and valid_password):
                return JSONResponse(
                    {"detail": "Authentication required."},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="AI Crypto Trader"'},
                )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


class PaperOpen(BaseModel):
    exchange: str = "binance"
    symbol: str = Field(min_length=3, max_length=30)
    market_type: str = "spot"
    side: str
    entry: float = Field(gt=0, allow_inf_nan=False)
    stop: float = Field(gt=0, allow_inf_nan=False)
    target: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    signal_id: int | None = None

    @model_validator(mode="after")
    def validate_trade(self):
        if self.exchange not in EXCHANGE_IDS:
            raise ValueError("Unsupported exchange")
        if self.market_type not in ("spot", "futures"):
            raise ValueError("market_type must be spot or futures")
        if self.side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
        if self.market_type == "spot" and self.side == "SHORT":
            raise ValueError("SHORT paper positions are not supported for spot markets")
        if self.side == "LONG" and self.stop >= self.entry:
            raise ValueError("LONG stop must be below entry")
        if self.side == "SHORT" and self.stop <= self.entry:
            raise ValueError("SHORT stop must be above entry")
        if self.target is not None:
            if self.side == "LONG" and self.target <= self.entry:
                raise ValueError("LONG target must be above entry")
            if self.side == "SHORT" and self.target >= self.entry:
                raise ValueError("SHORT target must be below entry")
        return self


class PaperClose(BaseModel):
    exit_price: float = Field(gt=0, allow_inf_nan=False)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.1.0", "mode": "paper-only"}


@app.get("/api/exchanges")
def exchanges():
    return {"exchanges": list(EXCHANGE_IDS), "connection": "public market data only"}


@app.get("/api/instruments/{exchange}/{symbol:path}")
async def instrument(exchange, symbol, market_type="spot"):
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    try:
        return await registry.market_spec(exchange, symbol, market_type)
    except Exception as exc:
        raise HTTPException(400, f"Unable to resolve exchange instrument: {exc}")


@app.get("/api/markets/{exchange}/{symbol:path}")
async def market(exchange, symbol, market_type="spot"):
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    try:
        resolved_symbol = await registry.resolve_symbol(exchange, symbol, market_type)
        ticker = await registry.ticker(exchange, resolved_symbol, market_type)
        return {
            "exchange": exchange, "symbol": resolved_symbol, "market_type": market_type,
            "last": ticker.get("last"), "bid": ticker.get("bid"),
            "ask": ticker.get("ask"), "base_volume": ticker.get("baseVolume"),
            "timestamp": ticker.get("timestamp"),
        }
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/backtest")
async def backtest(exchange="binance", symbol="BTC/USDT", market_type="spot", timeframe="1h", limit: int = 1000):
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    if limit < 52 or limit > 5000:
        raise HTTPException(400, "limit must be between 52 and 5000")
    try:
        resolved_symbol = await registry.resolve_symbol(exchange, symbol, market_type)
        candles = await registry.ohlcv(exchange, resolved_symbol, timeframe, limit, market_type)
        # The final exchange candle may still be forming; never use it for signals.
        return {"exchange": exchange, "symbol": resolved_symbol, "market_type": market_type,
                "timeframe": timeframe, **run_backtest(candles[:-1])}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/scan")
async def run_scan(exchange="binance", market_type="spot", symbols=None, timeframe="1h"):
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    if symbols and len([s for s in symbols.split(",") if s.strip()]) > 20:
        raise HTTPException(400, "A scan can contain at most 20 symbols")
    try:
        return {"results": await scan(
            exchange, [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
            market_type, timeframe
        )}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/signals")
def signals(limit: int = 100):
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    return {"signals": [dict(row) for row in rows]}


@app.get("/api/account")
async def get_account():
    data = account()
    positions_data = await get_positions()
    priced = [p for p in positions_data["positions"] if p.get("unrealized_pnl") is not None]
    missing = len(priced) != len(positions_data["positions"])
    unrealized = sum(float(p["unrealized_pnl"]) for p in priced)
    data.update({
        "unrealized_pnl": unrealized,
        "equity": float(data["starting_balance"]) + float(data["realized_pnl"]) + unrealized,
        "unpriced_open_positions": len(positions_data["positions"]) - len(priced),
        "equity_is_partial": missing,
        "unrealized_pnl_note": "Net estimate includes configured entry/exit fees and slippage; positions without live prices are excluded." if missing else "Net unrealized estimate includes configured fees and slippage; not an exchange account balance.",
    })
    return data


@app.get("/api/opportunities")
async def opportunities(limit: int = 12):
    """Latest distinct scanner setups, refreshed with current public ticker prices."""
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT 100"
        ).fetchall()]
    latest = []
    seen = set()
    for row in rows:
        key = (row["exchange"], row["symbol"], row["market_type"], row["timeframe"])
        if key in seen:
            continue
        seen.add(key)
        latest.append(row)
        if len(latest) >= max(1, min(limit, 30)):
            break

    async def mark_signal(row):
        try:
            ticker = await registry.ticker(row["exchange"], row["symbol"], row["market_type"])
            last = ticker.get("last")
            if last is None or float(last) <= 0:
                raise ValueError("No valid live price")
            return {
                "exchange": row["exchange"], "symbol": row["symbol"],
                "market_type": row["market_type"], "timeframe": row["timeframe"],
                "signal": row["signal"], "trend": row["trend"], "score": row["score"],
                "rsi": row["rsi"], "live_price": float(last),
                "signal_price": row["price"], "price_timestamp": ticker.get("timestamp"),
                "price_error": None,
            }
        except Exception as exc:
            return {
                "exchange": row["exchange"], "symbol": row["symbol"],
                "market_type": row["market_type"], "timeframe": row["timeframe"],
                "signal": row["signal"], "trend": row["trend"], "score": row["score"],
                "rsi": row["rsi"], "live_price": None, "signal_price": row["price"],
                "price_timestamp": None, "price_error": str(exc),
            }

    marked = await __import__("asyncio").gather(*(mark_signal(row) for row in latest))
    marked.sort(key=lambda item: (int(item["score"] or 0), item["live_price"] is not None), reverse=True)
    return {"opportunities": marked}



@app.get("/api/positions")
async def get_positions():
    """Return open paper positions marked to the latest public exchange ticker."""
    with connect() as db:
        rows = [
            dict(row) for row in db.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC"
            ).fetchall()
        ]

    async def mark_position(row):
        try:
            ticker = await registry.ticker(
                row["exchange"], row["symbol"], row["market_type"]
            )
            last = ticker.get("last")
            if last is None or not math.isfinite(float(last)) or float(last) <= 0:
                raise ValueError("Exchange did not return a valid last price")
            last = float(last)
            pnl_metrics = position_pnl(row["entry"], row["quantity"], row["side"], last, row.get("contract_size", 1.0), row.get("contract_type", "spot"))
            unrealized = pnl_metrics["unrealized_pnl"]
            stop_gap = abs(last - float(row["stop"]))
            stop_span = abs(float(row["entry"]) - float(row["stop"]))
            target = row.get("target")
            target_gap = abs(last - float(target)) if target is not None else None
            target_span = abs(float(target) - float(row["entry"])) if target is not None else None
            stop_proximity = max(0.0, 1.0 - stop_gap / stop_span) if stop_span else 0.0
            target_proximity = max(0.0, 1.0 - target_gap / target_span) if target_gap is not None and target_span else None
            alert = None
            exit_reason = None
            if (row["side"] == "LONG" and last <= float(row["stop"])) or (row["side"] == "SHORT" and last >= float(row["stop"])):
                alert = "STOP_REACHED"
                exit_reason = "STOP"
            elif target is not None and ((row["side"] == "LONG" and last >= float(target)) or (row["side"] == "SHORT" and last <= float(target))):
                alert = "TARGET_REACHED"
                exit_reason = "TARGET"
            elif stop_proximity >= 0.8:
                alert = "NEAR_STOP"
            elif target_proximity is not None and target_proximity >= 0.8:
                alert = "NEAR_TARGET"
            # This endpoint is read-only: the independent server monitor is
            # the sole automatic SL/TP closer. Keep alerts visible while it
            # processes the same observed market price.
            return {
                **row,
                "status": row["status"],
                "exit_reason": exit_reason,
                "exit_result": None,
                "live_price": last,
                **pnl_metrics,
                "stop_distance": stop_gap,
                "stop_distance_pct": stop_gap / last * 100,
                "target_distance": target_gap,
                "target_distance_pct": (target_gap / last * 100) if target_gap is not None else None,
                "stop_proximity_pct": stop_proximity * 100,
                "target_proximity_pct": target_proximity * 100 if target_proximity is not None else None,
                "alert": alert,
                "price_timestamp": ticker.get("timestamp"),
                "price_error": None,
            }
        except Exception as exc:
            return {
                **row,
                "live_price": None,
                "unrealized_pnl": None,
                "unrealized_pnl_pct": None,
                "price_timestamp": None,
                "price_error": str(exc),
            }

    return {"positions": await __import__("asyncio").gather(
        *(mark_position(row) for row in rows)
    )}


@app.get("/api/risk")
async def risk():
    marked = await get_positions()
    positions = marked["positions"]
    prices_complete = all(p.get("unrealized_pnl") is not None for p in positions)
    floating = sum(float(p["unrealized_pnl"]) for p in positions if p.get("unrealized_pnl") is not None)
    with connect() as db:
        today_realized = float(db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
            "WHERE status='CLOSED' AND date(closed_at)=date('now')"
        ).fetchone()["total"] or 0)
        snapshot = db.execute(
            "SELECT starting_equity FROM daily_equity_snapshots WHERE trading_date=date('now')"
        ).fetchone()
        realized_all = float(db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE status='CLOSED'"
        ).fetchone()["total"] or 0)
    daily_start = float(snapshot["starting_equity"]) if snapshot else settings.paper_starting_balance + realized_all - today_realized
    daily_pnl = today_realized + floating
    open_risk = sum(position_risk(p["entry"], p["stop"], p["quantity"], p.get("contract_size", 1.0), p.get("contract_type", "spot")) for p in positions)
    sizing_equity = settings.paper_starting_balance + realized_all
    equity = sizing_equity + floating
    remaining_daily_loss = max(0.0, daily_start * risk_manager.max_daily_loss_pct / 100 + daily_pnl)
    available_open_risk = max(0.0, sizing_equity * risk_manager.max_open_risk_pct / 100 - open_risk)
    return {
        "risk_per_trade_pct": risk_manager.risk_pct,
        "max_open_risk_pct": risk_manager.max_open_risk_pct,
        "max_daily_loss_pct": risk_manager.max_daily_loss_pct,
        "max_position_notional_pct": risk_manager.max_notional_pct,
        "max_open_trades": 3,
        "open_trades": len(positions),
        "open_risk": open_risk,
        "realized_pnl": realized_all,
        "unrealized_pnl": floating,
        "equity": equity,
        "available_open_risk": available_open_risk,
        "daily_pnl": daily_pnl,
        "remaining_daily_loss": remaining_daily_loss,
        "prices_complete": prices_complete,
        "status": "BLOCKED" if not prices_complete or remaining_daily_loss <= 0 or len(positions) >= 3 else "ACTIVE",
        "reward_risk_ratio": 2.0,
    }


@app.post("/api/paper/open")
async def paper_open(body: PaperOpen):
    try:
        # Resolve the symbol against the selected exchange/market before accepting
        # a manually entered paper position. This prevents arbitrary symbols and
        # spot/futures market mismatches from entering the journal.
        instrument = await registry.market_spec(body.exchange, body.symbol, body.market_type)
        resolved_symbol = instrument.get("symbol", body.symbol)
        ticker = await registry.ticker(body.exchange, resolved_symbol, body.market_type)
        last = ticker.get("last")
        if last is None or not math.isfinite(float(last)) or float(last) <= 0:
            raise ValueError("Exchange returned an invalid market price")
        last = float(last)
        deviation_pct = abs(float(body.entry) - last) / last * 100
        max_deviation_pct = float(settings.paper_entry_max_deviation_pct)
        if deviation_pct > max_deviation_pct:
            raise ValueError(
                f"Entry price is {deviation_pct:.2f}% away from live market price; "
                f"maximum allowed deviation is {max_deviation_pct:.2f}%"
            )
        marked = await get_positions()
        positions = marked["positions"]
        prices_complete = all(p.get("unrealized_pnl") is not None for p in positions)
        floating_pnl = sum(float(p["unrealized_pnl"]) for p in positions if p.get("unrealized_pnl") is not None)
        trade_data = body.model_dump()
        trade_data["symbol"] = resolved_symbol
        return open_trade(
            **trade_data,
            unrealized_pnl=floating_pnl,
            prices_complete=prices_complete,
            instrument=instrument,
        )
    except Exception as exc:
        raise HTTPException(400, f"Unable to validate/open paper trade: {exc}")


@app.post("/api/paper/{trade_id}/close")
def paper_close(trade_id: int, body: PaperClose):
    try:
        return close_trade(trade_id, body.exit_price)
    except Exception as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/journal")
def get_journal():
    return journal()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
