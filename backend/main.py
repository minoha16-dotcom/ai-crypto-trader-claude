import base64
import hmac
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


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield
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
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
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
    exit_price: float = Field(gt=0)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.1.0", "mode": "paper-only"}


@app.get("/api/exchanges")
def exchanges():
    return {"exchanges": list(EXCHANGE_IDS), "connection": "public market data only"}


@app.get("/api/markets/{exchange}/{symbol:path}")
async def market(exchange, symbol, market_type="spot"):
    if exchange not in EXCHANGE_IDS:
        raise HTTPException(400, "Unsupported exchange")
    if market_type not in ("spot", "futures"):
        raise HTTPException(400, "market_type must be spot or futures")
    try:
        ticker = await registry.ticker(exchange, symbol, market_type)
        return {
            "exchange": exchange, "symbol": symbol, "market_type": market_type,
            "last": ticker.get("last"), "bid": ticker.get("bid"),
            "ask": ticker.get("ask"), "base_volume": ticker.get("baseVolume"),
            "timestamp": ticker.get("timestamp"),
        }
    except Exception as exc:
        raise HTTPException(502, str(exc))


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
def get_account():
    return account()



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
            if last is None or float(last) <= 0:
                raise ValueError("Exchange did not return a valid last price")
            last = float(last)
            direction = 1 if row["side"] == "LONG" else -1
            unrealized = (last - float(row["entry"])) * float(row["quantity"]) * direction
            notional = float(row["entry"]) * float(row["quantity"])
            return {
                **row,
                "live_price": last,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (unrealized / notional * 100) if notional else 0,
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
def risk():
    return {
        "risk_per_trade_pct": risk_manager.risk_pct,
        "max_open_risk_pct": risk_manager.max_open_risk_pct,
        "max_daily_loss_pct": risk_manager.max_daily_loss_pct,
        "max_position_notional_pct": risk_manager.max_notional_pct,
    }


@app.post("/api/paper/open")
def paper_open(body: PaperOpen):
    try:
        return open_trade(**body.model_dump())
    except Exception as exc:
        raise HTTPException(400, str(exc))


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
