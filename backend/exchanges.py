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

    async def _load_markets(self, exchange_id, market_type):
        client = await self.get(exchange_id, market_type)
        key = (exchange_id, market_type)
        if key not in self._markets_loaded:
            await client.load_markets()
            self._markets_loaded.add(key)
        return client

    async def resolve_symbol(self, exchange_id, symbol, market_type="spot"):
        """Resolve common BASE/QUOTE input to one canonical CCXT market symbol."""
        client = await self._load_markets(exchange_id, market_type)
        requested = str(symbol).strip()
        if not requested:
            raise ValueError("A market symbol is required")

        markets = client.markets or {}
        exact = markets.get(requested)
        if exact is not None:
            return exact.get("symbol", requested)

        if market_type == "spot":
            # Let CCXT provide its normal, exchange-specific invalid-symbol error.
            return client.market(requested)["symbol"]

        pair, separator, requested_settle = requested.partition(":")
        if "/" not in pair:
            raise ValueError(
                f"Futures symbol '{requested}' must use BASE/QUOTE format"
            )
        base, quote = (part.strip().upper() for part in pair.split("/", 1))
        candidates = [
            market for market in markets.values()
            if market.get("contract")
            and (market.get("swap") or market.get("future"))
            and str(market.get("base", "")).upper() == base
            and str(market.get("quote", "")).upper() == quote
            and market.get("active") is not False
        ]

        if separator:
            candidates = [
                market for market in candidates
                if str(market.get("settle", "")).upper() == requested_settle.upper()
            ]
        else:
            # An unqualified pair prefers a linear contract settled in its quote
            # currency (for example BTC/USDT:USDT), when that market exists.
            quote_settled_linear = [
                market for market in candidates
                if market.get("linear")
                and str(market.get("settle", "")).upper() == quote
            ]
            if quote_settled_linear:
                candidates = quote_settled_linear

        perpetuals = [market for market in candidates if market.get("swap")]
        if perpetuals:
            candidates = perpetuals

        if not candidates:
            raise ValueError(
                f"No supported Futures contract matches '{requested}' on {exchange_id}"
            )
        if len(candidates) > 1:
            choices = ", ".join(sorted(market.get("symbol", "?") for market in candidates))
            raise ValueError(
                f"Futures symbol '{requested}' is ambiguous on {exchange_id}; "
                f"specify a settlement suffix. Matches: {choices}"
            )
        return candidates[0]["symbol"]

    async def market_spec(self, exchange_id, symbol, market_type="spot"):
        client = await self._load_markets(exchange_id, market_type)
        resolved_symbol = await self.resolve_symbol(exchange_id, symbol, market_type)
        market = client.market(resolved_symbol)
        spec = validate_instrument(market_type, market)
        spec["precision_mode"] = client.precisionMode
        spec["symbol"] = market["symbol"]
        spec["exchange"] = exchange_id
        spec["market_type"] = market_type
        return spec

    async def ticker(self, exchange_id, symbol, market_type="spot"):
        client = await self._load_markets(exchange_id, market_type)
        resolved_symbol = await self.resolve_symbol(exchange_id, symbol, market_type)
        return await client.fetch_ticker(resolved_symbol)

    async def ohlcv(self, exchange_id, symbol, timeframe="1h", limit=150, market_type="spot"):
        client = await self._load_markets(exchange_id, market_type)
        resolved_symbol = await self.resolve_symbol(exchange_id, symbol, market_type)
        return await client.fetch_ohlcv(
            resolved_symbol, timeframe=timeframe, limit=limit
        )

    async def close(self):
        await asyncio.gather(*(client.close() for client in self._clients.values()), return_exceptions=True)
        self._clients.clear()
        self._markets_loaded.clear()


registry = ExchangeRegistry()
