"""Opt-in live smoke check against public exchange market-data APIs.

Run: python scripts/check_exchanges.py
No API keys are used and no orders are created.
"""
import asyncio
import ccxt.async_support as ccxt

EXCHANGES = ("binance", "bybit", "okx")


async def check(exchange_id):
    exchange = getattr(ccxt, exchange_id)({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })
    try:
        await exchange.load_markets()
        market = exchange.market("BTC/USDT")
        ticker = await exchange.fetch_ticker(market["symbol"])
        if ticker.get("last") is None or ticker["last"] <= 0:
            raise RuntimeError("Ticker response did not contain a valid last price")
        return {"exchange": exchange_id, "status": "ok", "symbol": market["symbol"], "last": ticker["last"]}
    finally:
        await exchange.close()


async def main():
    results = await asyncio.gather(
        *(check(exchange_id) for exchange_id in EXCHANGES),
        return_exceptions=True,
    )
    failed = False
    for exchange_id, result in zip(EXCHANGES, results):
        if isinstance(result, Exception):
            failed = True
            print(f"FAIL {exchange_id}: {type(result).__name__}: {result}")
        else:
            print(f"PASS {result['exchange']} {result['symbol']} last={result['last']}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
