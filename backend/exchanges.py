"""Public market-data adapters only. No exchange credentials or private trading methods."""
import asyncio
import ccxt.async_support as ccxt

from .contracts import validate_instrument

EXCHANGE_IDS = ("binance", "bybit", "okx")
MARKET_TYPES = ("spot", "futures")


class ExchangeRegistry:
    def __init__(self):
        self._clients = {}
        self._markets_loaded = set()

    async def get(self, exchange_id, market_type="spot"):
        if exchange_id not in EXCHANGE_IDS:
            raise ValueError("Unsupported exchange: " + str(exchange_id))
        if market_type not in MARKET_TYPES:
            raise ValueError("market_type must be spot or futures")
        key = (exchange_id, market_type)
        if key not in self._clients:
            klass = getattr(ccxt, exchange_id)
            options = {"defaultType": "swap" if market_type == "futures" else "spot"}
            self._clients[key] = klass({"enableRateLimit": True, "timeout": 15000, "options": options})
        return self._clients[key]

    async def market_spec(self, exchange_id, symbol, market_type="spot"):
        client = await self.get(exchange_id, market_type)
        key = (exchange_id, market_type)
        if key not in self._markets_loaded:
            await client.load_markets()
            self._markets_loaded.add(key)
        market = client.market(symbol)
        spec = validate_instrument(market_type, market)
        spec["precision_mode"] = client.precisionMode
        spec["symbol"] = market["symbol"]
        spec["exchange"] = exchange_id
        spec["market_type"] = market_type
        return spec

    async def ticker(self, exchange_id, symbol, market_type="spot"):
        return await (await self.get(exchange_id, market_type)).fetch_ticker(symbol)

    async def ohlcv(self, exchange_id, symbol, timeframe="1h", limit=150, market_type="spot"):
        return await (await self.get(exchange_id, market_type)).fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    async def close(self):
        await asyncio.gather(*(client.close() for client in self._clients.values()), return_exceptions=True)
        self._clients.clear()
        self._markets_loaded.clear()


registry = ExchangeRegistry()
