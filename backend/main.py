import asyncio
import base64
import hmac
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator

from .config import settings
from .database import init_db, connect
from .exchanges import registry, EXCHANGE_IDS, ExchangeRateLimitCooldown
from .scanner import scan
from .paper import open_trade, close_trade, journal, account
from .risk import risk_manager
from .backtest import run_backtest
from .position_monitor import run_position_monitor
from .metrics import position_pnl, position_risk
from .advisor import analyze_closed_trade_history
from .version import APP_VERSION


@asynccontextmanager
async def lifespan(app):
    _require_dashboard_credentials()
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
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _dashboard_credentials():
    """Read dashboard credentials from process env, falling back to loaded .env settings."""
    return (
        os.getenv("DASHBOARD_USERNAME", settings.dashboard_username),
        os.getenv("DASHBOARD_PASSWORD", settings.dashboard_password),
    )


def _require_dashboard_credentials():
    username, password = _dashboard_credentials()
    if not username or not password:
        raise RuntimeError("DASHBOARD_USERNAME and DASHBOARD_PASSWORD must both be configured.")


def _load_open_positions():
    with connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC"
        ).fetchall()]


def _load_opportunity_signals():
    with connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM signals WHERE archived_at IS NULL ORDER BY id DESC LIMIT 100"
        ).fetchall()]


def _load_risk_snapshot():
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
    return today_realized, dict(snapshot) if snapshot else None, realized_all


@app.exception_handler(ExchangeRateLimitCooldown)
async def exchange_rate_limit_handler(request: Request, exc: ExchangeRateLimitCooldown):
    retry_after = str(max(1, int(exc.retry_after)))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": str(exc),
            "exchange": exc.exchange_id,
            "upstream_status": exc.status_code,
            "retry_after_seconds": int(retry_after),
        },
        headers={
            "Retry-After": retry_after,
            "X-Exchange-RateLimit-Status": str(exc.status_code),
        },
    )


@app.middleware("http")
async def dashboard_auth_and_security_headers(request: Request, call_next):
    # Render health checks stay public; every dashboard/API route requires Basic Auth.
    if request.url.path not in ("/api/health", "/api/ready"):
        username, password = _dashboard_credentials()
        if not username or not password:
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
    exchange: str = "kucoin"
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
    """Liveness only: confirms the web process can answer HTTP requests."""
    return {"status": "ok", "version": APP_VERSION, "mode": "paper-only"}


@app.get("/api/ready")
def readiness():
    """Readiness check: verify PostgreSQL/SQLite connectivity and required schema."""
    try:
        with connect() as db:
            for table in ("signals", "trades", "daily_equity_snapshots"):
                db.execute(f"SELECT 1 FROM {table} LIMIT 0")
    except Exception:
        # Do not disclose connection strings, hostnames, or database error details.
        return JSONResponse(
            {"status": "not_ready", "database": "unavailable"},
            status_code=503,
        )
    return {"status": "ready", "database": "ok", "version": APP_VERSION}


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
    except ExchangeRateLimitCooldown:
        raise
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
    except ExchangeRateLimitCooldown:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/candles/{exchange}/{symbol:path}")
async def market_candles(
    exchange,
    symbol,
    market_type="spot",
    timeframe="1h",
    limit: int = 80,
):
    """Return a bounded set of public OHLCV candles for the dashboard chart."""
    supported_timeframes = {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h",
        "4h", "6h", "12h", "1d", "1w",
    }
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    if timeframe not in supported_timeframes:
        raise HTTPException(400, "Unsupported candle timeframe")
    if limit < 20 or limit > 200:
        raise HTTPException(400, "Candle limit must be between 20 and 200")
    try:
        resolved_symbol = await registry.resolve_symbol(
            exchange, symbol, market_type
        )
        raw_candles = await registry.ohlcv(
            exchange, resolved_symbol, timeframe, limit, market_type
        )
        candles = []
        for candle in raw_candles:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            try:
                timestamp = int(candle[0])
                open_price, high, low, close, volume = (
                    float(value) for value in candle[1:6]
                )
            except (TypeError, ValueError, OverflowError):
                continue
            values = (open_price, high, low, close, volume)
            if not all(math.isfinite(value) for value in values):
                continue
            if min(open_price, high, low, close) <= 0 or volume < 0:
                continue
            if low > min(open_price, close) or high < max(open_price, close):
                continue
            if high < low:
                continue
            candles.append({
                "timestamp": timestamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            })
        return {
            "exchange": exchange,
            "symbol": resolved_symbol,
            "market_type": market_type,
            "timeframe": timeframe,
            "candles": candles,
        }
    except ExchangeRateLimitCooldown:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/backtest")
async def backtest(exchange="kucoin", symbol="BTC/USDT", market_type="spot", timeframe="1h", limit: int = 1000, volume_ratio_threshold: float | None = None):
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    if limit < 53 or limit > 5000:
        raise HTTPException(400, "limit must be between 53 and 5000 so 52 closed candles remain after excluding the forming candle")
    if volume_ratio_threshold is not None and (
        not math.isfinite(volume_ratio_threshold) or volume_ratio_threshold < 0
    ):
        raise HTTPException(400, "volume_ratio_threshold must be finite and non-negative")
    try:
        resolved_symbol = await registry.resolve_symbol(exchange, symbol, market_type)
        # Both Spot and Futures sizing must use the selected venue's real amount
        # precision and amount/cost limits.
        instrument = await registry.market_spec(
            exchange, resolved_symbol, market_type
        )
        candles, candle_meta = await registry.ohlcv_with_metadata(
            exchange, resolved_symbol, timeframe, limit, market_type
        )
        # The final exchange candle may still be forming; never use it for signals.
        closed_candles = candles[:-1]
        if len(closed_candles) < 52:
            return JSONResponse(status_code=422, content={
                "detail": "Exchange returned too few closed candles for a backtest.",
                "exchange": exchange,
                "symbol": resolved_symbol,
                "market_type": market_type,
                "timeframe": timeframe,
                "candle_data": candle_meta,
                "requested_candles": candle_meta["requested"],
                "received_candles": candle_meta["received"],
                "candle_shortfall": candle_meta["shortfall"],
                "usable_closed_candles": len(closed_candles),
            })
        return {
            "exchange": exchange,
            "symbol": resolved_symbol,
            "market_type": market_type,
            "timeframe": timeframe,
            "candle_data": candle_meta,
            "requested_candles": candle_meta["requested"],
            "received_candles": candle_meta["received"],
            "candle_shortfall": candle_meta["shortfall"],
            **await run_in_threadpool(
                run_backtest, closed_candles, settings.paper_starting_balance, instrument,
                volume_ratio_threshold
            ),
        }
    except ExchangeRateLimitCooldown:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/scan")
async def run_scan(exchange="kucoin", market_type="spot", symbols=None, timeframe="1h"):
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
    except ExchangeRateLimitCooldown:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/ai/recommendations")
async def ai_recommendations(exchange="kucoin", market_type="spot", symbols=None, timeframe="1h"):
    """Scan public markets and rank candidates from local closed paper-trade outcomes."""
    scan_result = await run_scan(exchange, market_type, symbols, timeframe)
    try:
        return await run_in_threadpool(
            analyze_closed_trade_history,
            scan_result["results"], market_type, timeframe,
        )
    except Exception as exc:
        raise HTTPException(500, f"Local advisor could not rank opportunities: {exc}")


@app.get("/api/signals")
def signals(limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 500))
    offset = max(0, min(offset, 1_000_000))
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM signals WHERE archived_at IS NULL ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) AS total FROM signals WHERE archived_at IS NULL"
        ).fetchone()["total"]
    return {"signals": [dict(row) for row in rows], "pagination": {
        "limit": limit, "offset": offset, "total": total,
        "has_more": offset + len(rows) < total,
    }}


@app.get("/api/account")
async def get_account():
    data = await run_in_threadpool(account)
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
    rows = await run_in_threadpool(_load_opportunity_signals)
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
                "price_timestamp": None,
                "price_error": str(exc),
                "price_error_status": 429 if isinstance(exc, ExchangeRateLimitCooldown) else None,
                "retry_after_seconds": exc.retry_after if isinstance(exc, ExchangeRateLimitCooldown) else None,
            }

    marked = await __import__("asyncio").gather(*(mark_signal(row) for row in latest))
    marked.sort(key=lambda item: (int(item["score"] or 0), item["live_price"] is not None), reverse=True)
    return {"opportunities": marked}



@app.get("/api/positions")
async def get_positions():
    """Return positions marked to the latest public last-traded price.

    Stop/target checks are periodic best-effort paper monitoring, not exchange
    orders, a guaranteed execution service, or real-time protection. Paper exit
    price is the last-traded price observed by the monitor; a gap can produce a
    materially different result from the configured stop/target level.
    """
    rows = await run_in_threadpool(_load_open_positions)

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
                "price_error_status": 429 if isinstance(exc, ExchangeRateLimitCooldown) else None,
                "retry_after_seconds": exc.retry_after if isinstance(exc, ExchangeRateLimitCooldown) else None,
            }

    return {
        "positions": await __import__("asyncio").gather(
            *(mark_position(row) for row in rows)
        ),
        "monitoring_note": (
            "Periodic best-effort checks of public last-traded prices; not real-time, "
            "not guaranteed, and no exchange stop/target orders are placed."
        ),
        "exit_price_model": "observed last-traded price at the monitor poll; gaps can cross the configured level",
    }


@app.get("/api/risk")
async def risk():
    marked = await get_positions()
    positions = marked["positions"]
    prices_complete = all(p.get("unrealized_pnl") is not None for p in positions)
    floating = sum(float(p["unrealized_pnl"]) for p in positions if p.get("unrealized_pnl") is not None)
    today_realized, snapshot, realized_all = await run_in_threadpool(_load_risk_snapshot)
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
        "max_open_trades": settings.max_open_trades,
        "open_trades": len(positions),
        "open_risk": open_risk,
        "realized_pnl": realized_all,
        "unrealized_pnl": floating,
        "equity": equity,
        "available_open_risk": available_open_risk,
        "daily_pnl": daily_pnl,
        "remaining_daily_loss": remaining_daily_loss,
        "prices_complete": prices_complete,
        "status": "BLOCKED" if not prices_complete or remaining_daily_loss <= 0 or len(positions) >= settings.max_open_trades else "ACTIVE",
        "reward_risk_ratio": settings.reward_risk_ratio,
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
        return await run_in_threadpool(
            open_trade,
            **trade_data,
            unrealized_pnl=floating_pnl,
            prices_complete=prices_complete,
            instrument=instrument,
        )
    except ExchangeRateLimitCooldown:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Unable to validate/open paper trade: {exc}")


@app.post("/api/paper/{trade_id}/close")
async def paper_close(trade_id: int, body: PaperClose):
    try:
        return await run_in_threadpool(close_trade, trade_id, body.exit_price)
    except ExchangeRateLimitCooldown:
        raise
    except Exception as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/journal")
def get_journal(limit: int = 100, offset: int = 0):
    """Paginated paper trade and signal history; old signals are soft archived."""
    return journal(limit=max(1, min(limit, 500)), offset=max(0, min(offset, 1_000_000)))


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

