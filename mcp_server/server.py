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


async def _configured_exchange(exchange: str | None) -> str:
    """Use an explicit supported exchange or the backend's configured default."""
    allowed = {"kucoin", "bybit", "okx"}
    if exchange is not None:
        if exchange not in allowed:
            raise ValueError("Unsupported exchange.")
        return exchange
    config = await _api("GET", "/api/exchanges")
    selected = config.get("default_exchange")
    if selected not in allowed:
        raise ValueError("Backend did not return a supported default exchange.")
    return selected


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
        if exc.response.status_code in (418, 429) and retry_after:
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
    exchange: Literal["kucoin", "bybit", "okx"],
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
    exchange: Literal["kucoin", "bybit", "okx"] | None = None,
    market_type: Literal["spot", "futures"] = "spot",
    symbols: str = "BTC/USDT,ETH/USDT",
    timeframe: Literal["5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h",
) -> dict:
    """Scan markets using the explicit exchange or backend DEFAULT_EXCHANGE."""
    exchange = await _configured_exchange(exchange)
    items = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not items or len(items) > 20 or any("/" not in item or len(item) > 30 for item in items):
        raise ValueError("Provide 1–20 CCXT symbols, e.g. BTC/USDT,ETH/USDT.")
    return await _api(
        "POST", "/api/scan",
        params={"exchange": exchange, "market_type": market_type,
                "symbols": ",".join(items), "timeframe": timeframe},
    )


@mcp.tool()
async def get_ai_recommendations(
    exchange: Literal["kucoin", "bybit", "okx"] | None = None,
    market_type: Literal["spot", "futures"] = "spot",
    symbols: str = "BTC/USDT,ETH/USDT",
    timeframe: Literal["5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h",
) -> dict:
    """Rank setups on the explicit exchange or backend DEFAULT_EXCHANGE."""
    exchange = await _configured_exchange(exchange)
    items = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    if not items or len(items) > 20 or any("/" not in item or len(item) > 30 for item in items):
        raise ValueError("Provide 1–20 CCXT symbols, e.g. BTC/USDT,ETH/USDT.")
    return await _api(
        "POST", "/api/ai/recommendations",
        params={"exchange": exchange, "market_type": market_type,
                "symbols": ",".join(items), "timeframe": timeframe},
    )


@mcp.tool()
async def get_instrument_spec(
    exchange: Literal["kucoin", "bybit", "okx"],
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
    exchange: Literal["kucoin", "bybit", "okx"],
    symbol: str,
    market_type: Literal["spot", "futures"] = "spot",
    timeframe: Literal["5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h",
    limit: int = 1000,
    volume_ratio_threshold: float | None = None,
    leverage: float = 1.0,
    funding_rate: float | None = None,
    funding_interval_hours: float | None = None,
    maintenance_margin_rate: float | None = None,
    liquidation_fee_rate: float | None = None,
) -> dict:
    """Run the historical paper backtest with optional isolated Futures assumptions."""
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    if not 53 <= limit <= 5000:
        raise ValueError("limit must be between 53 and 5000.")
    if volume_ratio_threshold is not None and (
        not math.isfinite(volume_ratio_threshold) or volume_ratio_threshold < 0
    ):
        raise ValueError("volume_ratio_threshold must be finite and non-negative.")
    params = {
        "exchange": exchange,
        "symbol": symbol,
        "market_type": market_type,
        "timeframe": timeframe,
        "limit": limit,
        "leverage": leverage,
    }
    if volume_ratio_threshold is not None:
        params["volume_ratio_threshold"] = volume_ratio_threshold
    if funding_rate is not None:
        params["funding_rate"] = funding_rate
    if funding_interval_hours is not None:
        params["funding_interval_hours"] = funding_interval_hours
    if maintenance_margin_rate is not None:
        params["maintenance_margin_rate"] = maintenance_margin_rate
    if liquidation_fee_rate is not None:
        params["liquidation_fee_rate"] = liquidation_fee_rate
    return await _api("GET", "/api/backtest", params=params)


@mcp.tool()
async def run_walk_forward(
    exchange: Literal["kucoin", "bybit", "okx"] | None = None,
    symbol: str = "BTC/USDT",
    market_type: Literal["spot", "futures"] = "spot",
    timeframe: Literal["5m", "15m", "30m", "1h", "2h", "4h", "1d"] = "1h",
    limit: int = 1500,
    train_bars: int = 250,
    validation_bars: int = 100,
    test_bars: int = 100,
    step_bars: int = 100,
    warmup_bars: int = 50,
    purge_bars: int = 20,
    embargo_bars: int | None = None,
    max_holding_bars: int = 20,
    window_mode: Literal["rolling", "expanding"] = "rolling",
    max_folds: int = 10,
    volume_ratio_threshold: float | None = None,
    max_missing_bars: int = 0,
    leverage: float = 1.0,
    funding_rate: float | None = None,
    funding_interval_hours: float | None = None,
    maintenance_margin_rate: float | None = None,
    liquidation_fee_rate: float | None = None,
) -> dict:
    """Run chronological paper-only evaluation with isolated Futures assumptions."""
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    if not 53 <= limit <= 5000:
        raise ValueError("limit must be between 53 and 5000.")
    if volume_ratio_threshold is not None and (
        not math.isfinite(volume_ratio_threshold) or volume_ratio_threshold < 0
    ):
        raise ValueError("volume_ratio_threshold must be finite and non-negative.")
    if isinstance(warmup_bars, bool) or not isinstance(warmup_bars, int) or warmup_bars < 50:
        raise ValueError("warmup_bars must be an integer of at least 50.")
    if (
        isinstance(max_holding_bars, bool)
        or not isinstance(max_holding_bars, int)
        or max_holding_bars < 1
    ):
        raise ValueError("max_holding_bars must be a positive integer.")
    if (
        isinstance(purge_bars, bool)
        or not isinstance(purge_bars, int)
        or purge_bars < max_holding_bars
    ):
        raise ValueError("purge_bars must be at least max_holding_bars.")
    required_embargo = max(warmup_bars, max_holding_bars)
    if embargo_bars is not None and (
        isinstance(embargo_bars, bool)
        or not isinstance(embargo_bars, int)
        or embargo_bars < required_embargo
    ):
        raise ValueError("embargo_bars must be at least max(warmup_bars, max_holding_bars).")
    exchange = await _configured_exchange(exchange)
    params = {
        "exchange": exchange,
        "symbol": symbol,
        "market_type": market_type,
        "timeframe": timeframe,
        "limit": limit,
        "train_bars": train_bars,
        "validation_bars": validation_bars,
        "test_bars": test_bars,
        "step_bars": step_bars,
        "warmup_bars": warmup_bars,
        "purge_bars": purge_bars,
        "max_holding_bars": max_holding_bars,
        "window_mode": window_mode,
        "max_folds": max_folds,
        "max_missing_bars": max_missing_bars,
        "leverage": leverage,
    }
    if funding_rate is not None:
        params["funding_rate"] = funding_rate
    if funding_interval_hours is not None:
        params["funding_interval_hours"] = funding_interval_hours
    if maintenance_margin_rate is not None:
        params["maintenance_margin_rate"] = maintenance_margin_rate
    if liquidation_fee_rate is not None:
        params["liquidation_fee_rate"] = liquidation_fee_rate
    if embargo_bars is not None:
        params["embargo_bars"] = embargo_bars
    if volume_ratio_threshold is not None:
        params["volume_ratio_threshold"] = volume_ratio_threshold
    return await _api("GET", "/api/walk-forward", params=params)


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
    exchange: Literal["kucoin", "bybit", "okx"],
    symbol: str,
    market_type: Literal["spot", "futures"],
    side: Literal["LONG", "SHORT"],
    entry: float,
    stop: float,
    target: float | None = None,
    leverage: float = 1.0,
    funding_rate: float | None = None,
    funding_interval_hours: float | None = None,
    maintenance_margin_rate: float | None = None,
    liquidation_fee_rate: float | None = None,
) -> dict:
    """Open a simulated paper position only; no exchange order is submitted.

    Spot SHORT positions are rejected. Futures use an isolated-margin model
    with assumed funding rates and an estimated liquidation trigger.
    """
    symbol = symbol.strip().upper()
    if not symbol or len(symbol) > 30 or "/" not in symbol:
        raise ValueError("Use a CCXT symbol such as BTC/USDT.")
    if side == "SHORT" and market_type == "spot":
        raise ValueError("Spot SHORT is not supported; choose Futures paper mode.")
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop)) or (target is not None and (not math.isfinite(target) or target <= 0)):
        raise ValueError("Entry, stop and target must be positive.")
    if not math.isfinite(leverage) or leverage < 1:
        raise ValueError("leverage must be finite and at least 1.")
    if market_type == "spot" and leverage != 1:
        raise ValueError("Spot paper positions do not support leverage.")
    optional_futures = {
        "funding_rate": funding_rate,
        "funding_interval_hours": funding_interval_hours,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_fee_rate": liquidation_fee_rate,
    }
    if any(
        value is not None and not math.isfinite(value)
        for value in optional_futures.values()
    ):
        raise ValueError("Futures funding and margin inputs must be finite.")
    payload = {
        "exchange": exchange, "symbol": symbol, "market_type": market_type,
        "side": side, "entry": entry, "stop": stop, "target": target,
        "leverage": leverage,
    }
    payload.update({
        key: value for key, value in optional_futures.items()
        if value is not None
    })
    return await _api("POST", "/api/paper/open", payload=payload)


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

