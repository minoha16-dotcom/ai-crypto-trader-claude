"""Private ChatGPT MCP bridge for AI Crypto Trader.

This server only calls the project's local FastAPI endpoints. It never connects
to exchange private APIs and never places live orders.
"""
from __future__ import annotations

import math
import os
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE_URL = os.getenv("TRADER_API_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("TRADER_API_TIMEOUT", "25"))

mcp = FastMCP(
    "AI Crypto Trader",
    instructions=(
        "Crypto market research and paper trading only. Exchange data is public. "
        "Never claim to place a live order. Opening and closing positions only "
        "create or update simulated paper trades in the configured database."
    ),
    host="127.0.0.1",
    port=int(os.getenv("MCP_PORT", "8001")),
    json_response=True,
)


async def _api(method: str, path: str, *, params: dict | None = None, payload: dict | None = None):
    """Call the local FastAPI service and return JSON with safe error handling."""
    try:
        username = os.getenv("TRADER_API_USERNAME", "")
        password = os.getenv("TRADER_API_PASSWORD", "")
        if bool(username) != bool(password):
            raise ValueError("Set both TRADER_API_USERNAME and TRADER_API_PASSWORD, or neither.")
        auth = (username, password) if username and password else None
        async with httpx.AsyncClient(
            base_url=API_BASE_URL, timeout=REQUEST_TIMEOUT, auth=auth
        ) as client:
            response = await client.request(method, path, params=params, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
            detail = body.get("detail", "Backend request failed")
        except Exception:
            body = {}
            detail = "Backend request failed"
        retry_after = exc.response.headers.get("Retry-After")
        if exc.response.status_code == 429 and retry_after:
            detail = f"{detail} (Retry-After: {retry_after}s)"
        raise ValueError(f"Trader backend rejected the request: {detail}") from None
    except httpx.RequestError:
        raise ValueError(
            "Cannot reach the local AI Crypto Trader API. Start it first at "
            f"{API_BASE_URL}."
        ) from None


@mcp.tool()
async def get_status() -> dict:
    """Check that the local trader backend is running in paper-only mode."""
    return await _api("GET", "/api/health")


@mcp.tool()
async def list_exchanges() -> dict:
    """List supported exchanges. Connections use public market data only."""
    return await _api("GET", "/api/exchanges")


@mcp.tool()
async def get_market_data(
    exchange: Literal["binance", "bybit", "okx"],
    symbol: str,
    market_type: Literal["spot", "futures"] = "spot",
) -> dict:
    """Fetch a public ticker for one symbol; does not access private accounts."""
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    return await _api("GET", f"/api/markets/{exchange}/{symbol}", params={"market_type": market_type})


@mcp.tool()
async def scan_opportunities(
    exchange: Literal["binance", "bybit", "okx"] = "binance",
    market_type: Literal["spot", "futures"] = "spot",
    symbols: str = "BTC/USDT,ETH/USDT",
    timeframe: Literal["5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h",
) -> dict:
    """Scan up to 20 comma-separated markets using the project's EMA/RSI/ATR rules."""
    items = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not items or len(items) > 20 or any("/" not in item or len(item) > 30 for item in items):
        raise ValueError("Provide 1–20 CCXT symbols, e.g. BTC/USDT,ETH/USDT.")
    return await _api(
        "POST", "/api/scan",
        params={"exchange": exchange, "market_type": market_type,
                "symbols": ",".join(items), "timeframe": timeframe},
    )


@mcp.tool()
async def get_instrument_spec(
    exchange: Literal["binance", "bybit", "okx"],
    symbol: str,
    market_type: Literal["spot", "futures"] = "spot",
) -> dict:
    """Get normalized public Spot/Futures contract size, precision and limits."""
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    return await _api(
        "GET", f"/api/instruments/{exchange}/{symbol}",
        params={"market_type": market_type},
    )


@mcp.tool()
async def get_open_positions() -> dict:
    """Read open paper positions with live marks, unrealized P&L and stop/target alerts."""
    return await _api("GET", "/api/positions")


@mcp.tool()
async def get_opportunities(limit: int = 12) -> dict:
    """Read the latest distinct scanner opportunities with refreshed public prices."""
    if not 1 <= limit <= 30:
        raise ValueError("limit must be between 1 and 30.")
    return await _api("GET", "/api/opportunities", params={"limit": limit})


@mcp.tool()
async def run_backtest(
    exchange: Literal["binance", "bybit", "okx"],
    symbol: str,
    market_type: Literal["spot", "futures"] = "spot",
    timeframe: Literal["5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h",
    limit: int = 1000,
) -> dict:
    """Run the backend's historical OHLCV paper backtest; does not submit orders."""
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    if not 53 <= limit <= 5000:
        raise ValueError("limit must be between 53 and 5000.")
    return await _api(
        "GET", "/api/backtest",
        params={
            "exchange": exchange,
            "symbol": symbol,
            "market_type": market_type,
            "timeframe": timeframe,
            "limit": limit,
        },
    )


@mcp.tool()
async def get_signals(limit: int = 30) -> dict:
    """Read recent scanner signals saved in the local database."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")
    return await _api("GET", "/api/signals", params={"limit": limit})


@mcp.tool()
async def get_paper_account() -> dict:
    """Read simulated account balance, realized PnL and open paper positions."""
    return await _api("GET", "/api/account")


@mcp.tool()
async def get_risk_limits() -> dict:
    """Read configured risk limits used by the paper-trading engine."""
    return await _api("GET", "/api/risk")


@mcp.tool()
async def get_trade_journal() -> dict:
    """Read paper trades and saved signals from the local journal."""
    return await _api("GET", "/api/journal")


@mcp.tool()
async def open_paper_trade(
    exchange: Literal["binance", "bybit", "okx"],
    symbol: str,
    market_type: Literal["spot", "futures"],
    side: Literal["LONG", "SHORT"],
    entry: float,
    stop: float,
    target: float | None = None,
) -> dict:
    """Open a simulated paper position only; no exchange order is submitted.

    Spot SHORT positions are rejected. Futures mode is a simplified simulation,
    not a full contract, funding, margin or liquidation model.
    """
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    if side == "SHORT" and market_type == "spot":
        raise ValueError("Spot SHORT is not supported; choose Futures paper mode.")
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop)) or (target is not None and (not math.isfinite(target) or target <= 0)):
        raise ValueError("Entry, stop and target must be positive.")
    return await _api("POST", "/api/paper/open", payload={
        "exchange": exchange, "symbol": symbol, "market_type": market_type,
        "side": side, "entry": entry, "stop": stop, "target": target,
    })


@mcp.tool()
async def close_paper_trade(trade_id: int, exit_price: float) -> dict:
    """Close an existing simulated paper position at the user-supplied exit price."""
    if trade_id <= 0 or not math.isfinite(exit_price) or exit_price <= 0:
        raise ValueError("Trade ID and exit price must be positive.")
    return await _api(
        "POST", f"/api/paper/{trade_id}/close",
        payload={"exit_price": exit_price},
    )


if __name__ == "__main__":
    # ChatGPT connects over Streamable HTTP at http://127.0.0.1:8001/mcp.
    mcp.run(transport="streamable-http")
