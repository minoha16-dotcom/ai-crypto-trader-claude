"""Public market-data adapters only. No exchange credentials or private trading methods."""
import asyncio
import copy
import math
import time

import ccxt.async_support as ccxt

from .contracts import validate_instrument

EXCHANGE_IDS = ("binance", "bybit", "okx")
MARKET_TYPES = ("spot", "futures")
DEFAULT_TICKER_CACHE_TTL_SECONDS = 2.0
MAX_TICKER_CACHE_ENTRIES = 512


class ExchangeRegistry:
    def __init__(self, ticker_cache_ttl=DEFAULT_TICKER_CACHE_TTL_SECONDS):
        if not isinstance(ticker_cache_ttl, (int, float)) or not math.isfinite(float(ticker_cache_ttl)) or ticker_cache_ttl < 0:
            raise ValueError("ticker_cache_ttl must be a finite non-negative number")
        self._clients = {}
        self._markets_loaded = set()
        # CCXT creates one throttler per client. Spot and Futures clients for the
        # same venue must share one throttler because exchanges commonly account
        # public REST request weight at the IP/platform level.
        self._shared_throttlers = {}
        self._ticker_cache_ttl = float(ticker_cache_ttl)
        self._ticker_cache = {}
        self._ticker_inflight = {}
        self._ticker_lock = asyncio.Lock()

    async def get(self, exchange_id, market_type="spot"):
        if exchange_id not in EXCHANGE_IDS:
            raise ValueError("Unsupported exchange: " + str(exchange_id))
        if market_type not in MARKET_TYPES:
            raise ValueError("market_type must be spot or futures")
        key = (exchange_id, market_type)
        if key not in self._clients:
            klass = getattr(ccxt, exchange_id)
            options = {"defaultType": "swap" if market_type == "futures" else "spot"}
            client = klass({"enableRateLimit": True, "timeout": 15000, "options": options})
            shared_throttler = self._shared_throttlers.get(exchange_id)
            client_throttler = getattr(client, "throttler", None)
            if client_throttler is None:
                raise RuntimeError(
                    f"CCXT did not initialize its rate limiter for {exchange_id}"
                )
            if shared_throttler is None:
                self._shared_throttlers[exchange_id] = client_throttler
            else:
                # Reuse CCXT's own weighted token-bucket throttler, preserving
                # each endpoint's CCXT-calculated request cost.
                client.throttler = shared_throttler
            self._clients[key] = client
        return self._clients[key]

    async def _load_markets(self, exchange_id, market_type):
        client = await self.get(exchange_id, market_type)
        key = (exchange_id, market_type)
        if key not in self._markets_loaded:
            await client.load_markets()
            self._markets_loaded.add(key)
        return client

    @staticmethod
    def _matches_market_type(market, market_type):
        """Do not mistake a same-pair Spot entry for a Futures contract."""
        if market_type == "spot":
            return bool(market.get("spot")) or market.get("type") == "spot"
        return bool(market.get("contract")) and bool(
            market.get("swap") or market.get("future")
        )

    async def resolve_symbol(self, exchange_id, symbol, market_type="spot"):
        """Resolve common BASE/QUOTE input to one canonical CCXT market symbol."""
        client = await self._load_markets(exchange_id, market_type)
        requested = str(symbol).strip()
        if not requested:
            raise ValueError("A market symbol is required")
        if market_type not in MARKET_TYPES:
            raise ValueError("market_type must be spot or futures")

        markets = client.markets or {}
        exact = markets.get(requested)
        if exact is not None and self._matches_market_type(exact, market_type):
            return exact.get("symbol", requested)

        if market_type == "spot":
            # Let CCXT resolve aliases, but reject a result of the wrong market type.
            market = client.market(requested)
            if not self._matches_market_type(market, "spot"):
                raise ValueError(f"Selected market '{requested}' is not a Spot market")
            return market["symbol"]

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

    async def _fetch_ticker_and_cache(self, key, client, symbol):
        current_task = asyncio.current_task()
        try:
            ticker = await client.fetch_ticker(symbol)
            if self._ticker_cache_ttl > 0:
                cached_value = copy.deepcopy(ticker)
                async with self._ticker_lock:
                    now = time.monotonic()
                    self._ticker_cache[key] = (
                        now + self._ticker_cache_ttl,
                        cached_value,
                    )
                    # Bound memory use for long-running services that scan many
                    # symbols. Expired entries are preferred for eviction.
                    if len(self._ticker_cache) > MAX_TICKER_CACHE_ENTRIES:
                        expired = [
                            cache_key for cache_key, (expires_at, _) in self._ticker_cache.items()
                            if expires_at <= now
                        ]
                        for cache_key in expired:
                            self._ticker_cache.pop(cache_key, None)
                        while len(self._ticker_cache) > MAX_TICKER_CACHE_ENTRIES:
                            self._ticker_cache.pop(next(iter(self._ticker_cache)), None)
            return ticker
        finally:
            async with self._ticker_lock:
                if self._ticker_inflight.get(key) is current_task:
                    self._ticker_inflight.pop(key, None)

    async def ticker(self, exchange_id, symbol, market_type="spot"):
        client = await self._load_markets(exchange_id, market_type)
        resolved_symbol = await self.resolve_symbol(exchange_id, symbol, market_type)
        # Include market type in the key: BTC/USDT Spot and BTC/USDT:USDT
        # Futures are different instruments and must never share cached prices.
        key = (exchange_id, market_type, resolved_symbol)
        now = time.monotonic()
        async with self._ticker_lock:
            cached = self._ticker_cache.get(key)
            if cached is not None:
                expires_at, ticker = cached
                if expires_at > now:
                    return copy.deepcopy(ticker)
                self._ticker_cache.pop(key, None)

            task = self._ticker_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._fetch_ticker_and_cache(key, client, resolved_symbol)
                )
                self._ticker_inflight[key] = task

        # Shield the shared fetch so one cancelled HTTP request handler does
        # not cancel the result awaited by other callers.
        return copy.deepcopy(await asyncio.shield(task))

    async def ohlcv(self, exchange_id, symbol, timeframe="1h", limit=150, market_type="spot"):
        client = await self._load_markets(exchange_id, market_type)
        resolved_symbol = await self.resolve_symbol(exchange_id, symbol, market_type)
        return await client.fetch_ohlcv(
            resolved_symbol, timeframe=timeframe, limit=limit
        )

    async def close(self):
        pending = list(self._ticker_inflight.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*(client.close() for client in self._clients.values()), return_exceptions=True)
        self._clients.clear()
        self._markets_loaded.clear()
        self._shared_throttlers.clear()
        self._ticker_cache.clear()
        self._ticker_inflight.clear()


registry = ExchangeRegistry()
