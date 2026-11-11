"""Public market-data adapters only. No exchange credentials or private trading methods."""
import asyncio
import copy
import logging
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import ccxt.async_support as ccxt

from .contracts import validate_instrument

EXCHANGE_IDS = ("binance", "bybit", "okx")
MARKET_TYPES = ("spot", "futures")
DEFAULT_TICKER_CACHE_TTL_SECONDS = 2.0
MAX_TICKER_CACHE_ENTRIES = 512
RATE_LIMIT_FALLBACK_SECONDS = {418: 300, 429: 60}
logger = logging.getLogger(__name__)


class ExchangeRateLimitCooldown(RuntimeError):
    """Raised locally while an exchange has asked this IP to slow down."""

    def __init__(self, exchange_id, status_code, retry_after):
        self.exchange_id = exchange_id
        self.status_code = status_code
        self.retry_after = max(1, int(math.ceil(retry_after)))
        super().__init__(
            f"{exchange_id} HTTP {status_code} rate-limit cooldown is active; "
            f"retry after {self.retry_after}s"
        )


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
        # The cooldown is shared by Spot and Futures because bans are normally
        # enforced against the exchange/IP, not one CCXT market client.
        self._rate_limit_until = {}
        self._rate_limit_status = {}

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
                # Check again after CCXT's queue delay. Requests already queued
                # when a 418/429 arrives must not leave the process during the
                # exchange's cooldown window.
                async def guarded_throttler(cost=None, _base=client_throttler, _exchange=exchange_id):
                    await _base(cost)
                    self._ensure_request_allowed(_exchange)

                self._shared_throttlers[exchange_id] = guarded_throttler
                client.throttler = guarded_throttler
            else:
                # Reuse the same weighted queue across Spot and Futures clients.
                client.throttler = shared_throttler
            self._clients[key] = client
        return self._clients[key]

    @staticmethod
    def _status_code_from_error(error):
        current = error
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            for attr in ("status_code", "http_status", "status"):
                value = getattr(current, attr, None)
                try:
                    code = int(value)
                except (TypeError, ValueError):
                    continue
                if code in (418, 429):
                    return code
            name = type(current).__name__.lower()
            if name == "ddosprotection":
                return 418
            if name == "ratelimitexceeded":
                return 429
            match = re.search(r"(?<!\\d)(418|429)(?!\\d)", str(current))
            if match:
                return int(match.group(1))
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        return None

    @staticmethod
    def _response_headers(client, error):
        candidates = [
            getattr(client, "last_response_headers", None),
            getattr(error, "headers", None),
            getattr(getattr(error, "response", None), "headers", None),
        ]
        current = getattr(error, "__cause__", None)
        while current is not None:
            candidates.extend([
                getattr(current, "headers", None),
                getattr(getattr(current, "response", None), "headers", None),
            ])
            current = getattr(current, "__cause__", None)
        for candidate in candidates:
            if candidate and hasattr(candidate, "items"):
                return {str(key).lower(): value for key, value in candidate.items()}
        return {}

    @staticmethod
    def _retry_after_from_headers(headers):
        raw = headers.get("retry-after")
        if raw is None:
            return None, None
        value = str(raw).strip()
        try:
            return max(0.0, float(value)), value
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, retry_at.timestamp() - time.time()), value
            except (TypeError, ValueError, OverflowError):
                return None, value

    @staticmethod
    def _ban_timestamp_delay(error_text):
        # Binance can return "IP banned until <epoch-ms>" in its JSON body.
        match = re.search(r"(?:IP\\s+)?banned\\s+until\\s+(\\d{10,13})", error_text, re.IGNORECASE)
        if not match:
            return None
        timestamp = int(match.group(1))
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return max(0.0, timestamp - time.time())

    def _activate_rate_limit(self, exchange_id, client, error, status_code):
        headers = self._response_headers(client, error)
        header_delay, header_value = self._retry_after_from_headers(headers)
        body = getattr(client, "last_http_response", None)
        details = " ".join(part for part in (str(error), str(body or "")) if part)
        ban_delay = self._ban_timestamp_delay(details)

        candidates = [value for value in (header_delay, ban_delay) if value is not None]
        if candidates:
            delay = max(candidates)
            source = "Retry-After header" if header_delay is not None else "response ban timestamp"
        else:
            delay = float(RATE_LIMIT_FALLBACK_SECONDS[status_code])
            source = "local fallback"
        # Keep an existing longer cooldown; a later shorter response must not
        # release this exchange/IP early.
        now = time.monotonic()
        previous_until = self._rate_limit_until.get(exchange_id, 0.0)
        until = max(previous_until, now + max(1.0, delay))
        self._rate_limit_until[exchange_id] = until
        self._rate_limit_status[exchange_id] = status_code
        remaining = max(1.0, until - now)
        retry_after_log = header_value if header_value is not None else "not supplied"
        logger.warning(
            "Exchange rate limit activated: exchange=%s HTTP=%s Retry-After=%s "
            "cooldown_seconds=%d source=%s",
            exchange_id, status_code, retry_after_log, int(math.ceil(remaining)), source,
        )
        return ExchangeRateLimitCooldown(exchange_id, status_code, remaining)

    def _ensure_request_allowed(self, exchange_id):
        until = self._rate_limit_until.get(exchange_id)
        if until is None:
            return
        remaining = until - time.monotonic()
        if remaining <= 0:
            self._rate_limit_until.pop(exchange_id, None)
            self._rate_limit_status.pop(exchange_id, None)
            logger.info("Exchange rate-limit cooldown expired: exchange=%s", exchange_id)
            return
        raise ExchangeRateLimitCooldown(
            exchange_id,
            self._rate_limit_status.get(exchange_id, 429),
            remaining,
        )

    async def _request(self, exchange_id, client, operation, *args, **kwargs):
        self._ensure_request_allowed(exchange_id)
        try:
            return await operation(*args, **kwargs)
        except Exception as error:
            status_code = self._status_code_from_error(error)
            if status_code in (418, 429):
                raise self._activate_rate_limit(
                    exchange_id, client, error, status_code
                ) from error
            raise

    async def _load_markets(self, exchange_id, market_type):
        client = await self.get(exchange_id, market_type)
        key = (exchange_id, market_type)
        if key not in self._markets_loaded:
            await self._request(exchange_id, client, client.load_markets)
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
            ticker = await self._request(
                key[0], client, client.fetch_ticker, symbol
            )
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
        return await self._request(
            exchange_id,
            client,
            client.fetch_ohlcv,
            resolved_symbol,
            timeframe=timeframe,
            limit=limit,
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
        self._rate_limit_until.clear()
        self._rate_limit_status.clear()


registry = ExchangeRegistry()
