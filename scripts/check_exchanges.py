"""Live smoke checks for public Spot and linear/inverse Futures markets.

Run: python scripts/check_exchanges.py
No API keys, private account endpoints, or orders are used.
"""
import asyncio
import math

import ccxt.async_support as ccxt

EXCHANGES = ("binance", "bybit", "okx")


def choose_market(exchange, market_type):
    if market_type == "spot":
        market = exchange.markets.get("BTC/USDT")
        if market and (market.get("spot") or market.get("type") == "spot"):
            return market
        raise RuntimeError("BTC/USDT Spot market was not listed")

    candidates = [
        market for market in exchange.markets.values()
        if market.get("contract")
        and (market.get("swap") or market.get("future"))
        and str(market.get("base", "")).upper() == "BTC"
        and str(market.get("quote", "")).upper() == "USDT"
        and market.get("active") is not False
    ]
    if not candidates:
        raise RuntimeError("No active BTC/USDT Futures contract was listed")

    quote_settled_linear = [
        market for market in candidates
        if market.get("linear")
        and str(market.get("settle", "")).upper() == "USDT"
    ]
    if quote_settled_linear:
        candidates = quote_settled_linear
    swaps = [market for market in candidates if market.get("swap")]
    if swaps:
        candidates = swaps
    candidates.sort(key=lambda market: market.get("symbol", ""))
    return candidates[0]


async def check(exchange_id, market_type):
    client = getattr(ccxt, exchange_id)({
        "enableRateLimit": True,
        "timeout": 25000,
        "options": {"defaultType": "spot" if market_type == "spot" else "swap"},
    })
    try:
        await client.load_markets()
        market = choose_market(client, market_type)
        if market_type == "futures":
            linear = bool(market.get("linear"))
            inverse = bool(market.get("inverse"))
            if linear == inverse:
                raise RuntimeError("Futures market must be explicitly linear or inverse")
            contract_size = market.get("contractSize")
            if contract_size is None or not math.isfinite(float(contract_size)) or float(contract_size) <= 0:
                raise RuntimeError("Futures market has invalid contractSize")
            amount_precision = (market.get("precision") or {}).get("amount")
            if amount_precision is None:
                raise RuntimeError("Futures market does not publish amount precision")
            contract_type = "linear" if linear else "inverse"
        else:
            contract_size = 1.0
            amount_precision = (market.get("precision") or {}).get("amount")
            contract_type = "spot"

        ticker = await client.fetch_ticker(market["symbol"])
        last = ticker.get("last")
        if last is None or not math.isfinite(float(last)) or float(last) <= 0:
            raise RuntimeError("Ticker response did not contain a finite positive last price")
        return {
            "exchange": exchange_id,
            "market_type": market_type,
            "symbol": market["symbol"],
            "last": float(last),
            "contract_type": contract_type,
            "contract_size": float(contract_size),
            "amount_precision": amount_precision,
        }
    finally:
        await client.close()


async def main():
    checks = [(exchange_id, market_type)
              for exchange_id in EXCHANGES
              for market_type in ("spot", "futures")]
    results = await asyncio.gather(
        *(check(exchange_id, market_type) for exchange_id, market_type in checks),
        return_exceptions=True,
    )
    failed = False
    for (exchange_id, market_type), result in zip(checks, results):
        if isinstance(result, Exception):
            failed = True
            print(f"FAIL {exchange_id} {market_type}: {type(result).__name__}: {result}")
        else:
            suffix = (
                f" contract={result['contract_type']} size={result['contract_size']} "
                f"amount_precision={result['amount_precision']}"
                if market_type == "futures" else ""
            )
            print(
                f"PASS {exchange_id} {market_type} {result['symbol']} "
                f"last={result['last']}{suffix}"
            )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
