import asyncio
import pytest

from backend.exchanges import ExchangeRegistry, EXCHANGE_IDS


@pytest.mark.parametrize("exchange_id", EXCHANGE_IDS)
def test_registry_rejects_invalid_market_type(exchange_id):
    async def run():
        registry = ExchangeRegistry()
        with pytest.raises(ValueError, match="market_type"):
            await registry.get(exchange_id, "margin")
        await registry.close()
    asyncio.run(run())


def test_registry_rejects_unknown_exchange():
    async def run():
        registry = ExchangeRegistry()
        with pytest.raises(ValueError, match="Unsupported exchange"):
            await registry.get("not-an-exchange", "spot")
        await registry.close()
    asyncio.run(run())


def test_registry_creates_distinct_clients_per_market():
    async def run():
        registry = ExchangeRegistry()
        spot = await registry.get("binance", "spot")
        futures = await registry.get("binance", "futures")
        assert spot is not futures
        assert spot.options["defaultType"] == "spot"
        assert futures.options["defaultType"] == "swap"
        await registry.close()
        assert registry._clients == {}
    asyncio.run(run())


def test_market_spec_loads_and_normalizes_contract_metadata(monkeypatch):
    class FakeClient:
        precisionMode = 2
        async def load_markets(self):
            return {}
        markets = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT", "contract": True, "swap": True,
                "linear": True, "inverse": False, "contractSize": 0.001,
                "precision": {"amount": 3},
                "limits": {"amount": {"min": 0.001, "max": 100000}},
                "base": "BTC", "quote": "USDT", "settle": "USDT",
            }
        }
        def market(self, symbol):
            return self.markets[symbol]
        async def close(self):
            pass

    async def run():
        registry = ExchangeRegistry()
        fake = FakeClient()
        async def fake_get(exchange_id, market_type):
            return fake
        monkeypatch.setattr(registry, "get", fake_get)
        spec = await registry.market_spec("bybit", "BTC/USDT", "futures")
        assert spec["contract_type"] == "linear"
        assert spec["contract_size"] == pytest.approx(0.001)
        assert spec["precision_mode"] == 2
        assert spec["min_amount"] == pytest.approx(0.001)
    asyncio.run(run())



def test_futures_symbol_alias_prefers_quote_settled_linear_perpetual():
    async def run():
        registry = ExchangeRegistry()
        class FakeClient:
            markets = {
                "BTC/USD:BTC": {
                    "symbol": "BTC/USD:BTC", "contract": True, "swap": True,
                    "future": False, "linear": False, "inverse": True,
                    "base": "BTC", "quote": "USD", "settle": "BTC", "active": True,
                },
                "BTC/USDT:USDT": {
                    "symbol": "BTC/USDT:USDT", "contract": True, "swap": True,
                    "future": False, "linear": True, "inverse": False,
                    "base": "BTC", "quote": "USDT", "settle": "USDT", "active": True,
                },
            }
            async def load_markets(self):
                return self.markets
            async def close(self):
                pass
        fake = FakeClient()
        async def fake_get(exchange_id, market_type):
            return fake
        registry.get = fake_get
        assert await registry.resolve_symbol("binance", "BTC/USDT", "futures") == "BTC/USDT:USDT"
        assert await registry.resolve_symbol("binance", "BTC/USD", "futures") == "BTC/USD:BTC"
        await registry.close()
    asyncio.run(run())


def test_futures_symbol_alias_rejects_ambiguous_settlement():
    async def run():
        registry = ExchangeRegistry()
        class FakeClient:
            markets = {
                "BTC/USD:USDT": {
                    "symbol": "BTC/USD:USDT", "contract": True, "swap": True,
                    "linear": True, "base": "BTC", "quote": "USD",
                    "settle": "USDT", "active": True,
                },
                "BTC/USD:USDC": {
                    "symbol": "BTC/USD:USDC", "contract": True, "swap": True,
                    "linear": True, "base": "BTC", "quote": "USD",
                    "settle": "USDC", "active": True,
                },
            }
            async def load_markets(self):
                return self.markets
            async def close(self):
                pass
        fake = FakeClient()
        async def fake_get(exchange_id, market_type):
            return fake
        registry.get = fake_get
        with pytest.raises(ValueError, match="ambiguous"):
            await registry.resolve_symbol("okx", "BTC/USD", "futures")
        assert await registry.resolve_symbol("okx", "BTC/USD:USDT", "futures") == "BTC/USD:USDT"
        await registry.close()
    asyncio.run(run())



def test_futures_symbol_does_not_match_same_pair_spot_market():
    async def run():
        registry = ExchangeRegistry()

        class FakeClient:
            precisionMode = 2
            markets = {
                "BTC/USDT": {
                    "symbol": "BTC/USDT", "type": "spot", "spot": True,
                    "contract": False, "base": "BTC", "quote": "USDT",
                    "active": True,
                },
                "BTC/USDT:USDT": {
                    "symbol": "BTC/USDT:USDT", "type": "swap", "spot": False,
                    "contract": True, "swap": True, "future": False,
                    "linear": True, "inverse": False, "contractSize": 0.001,
                    "precision": {"amount": 3},
                    "limits": {"amount": {"min": 0.001}},
                    "base": "BTC", "quote": "USDT", "settle": "USDT",
                    "active": True,
                },
            }

            async def load_markets(self):
                return self.markets

            def market(self, symbol):
                return self.markets[symbol]

            async def close(self):
                pass

        fake = FakeClient()

        async def fake_get(exchange_id, market_type):
            return fake

        registry.get = fake_get

        # The exact BTC/USDT key is Spot, but Futures resolution must continue
        # and select the canonical linear contract symbol instead.
        assert await registry.resolve_symbol(
            "binance", "BTC/USDT", "spot"
        ) == "BTC/USDT"
        assert await registry.resolve_symbol(
            "binance", "BTC/USDT", "futures"
        ) == "BTC/USDT:USDT"

        spec = await registry.market_spec("binance", "BTC/USDT", "futures")
        assert spec["symbol"] == "BTC/USDT:USDT"
        assert spec["contract_type"] == "linear"
        await registry.close()

    asyncio.run(run())



def test_registry_shares_rate_limiter_across_spot_and_futures(monkeypatch):
    from backend import exchanges as exchange_module

    class FakeClient:
        def __init__(self, config):
            self.options = config["options"]
            self.throttler = object()

        async def close(self):
            pass

    monkeypatch.setattr(exchange_module.ccxt, "binance", FakeClient)
    monkeypatch.setattr(exchange_module.ccxt, "bybit", FakeClient)

    async def run():
        registry = ExchangeRegistry()
        binance_spot = await registry.get("binance", "spot")
        binance_futures = await registry.get("binance", "futures")
        bybit_spot = await registry.get("bybit", "spot")

        assert binance_spot is not binance_futures
        assert binance_spot.throttler is binance_futures.throttler
        assert binance_spot.throttler is not bybit_spot.throttler
        assert binance_spot.options["defaultType"] == "spot"
        assert binance_futures.options["defaultType"] == "swap"
        await registry.close()

    asyncio.run(run())


def test_ticker_cache_deduplicates_concurrent_and_repeated_requests(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.fetch_count = 0

        async def fetch_ticker(self, symbol):
            self.fetch_count += 1
            await asyncio.sleep(0.01)
            return {"symbol": symbol, "last": 123.45}

    async def run():
        registry = ExchangeRegistry(ticker_cache_ttl=10)
        fake = FakeClient()

        async def fake_load_markets(exchange_id, market_type):
            return fake

        async def fake_resolve_symbol(exchange_id, symbol, market_type):
            return "BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT"

        monkeypatch.setattr(registry, "_load_markets", fake_load_markets)
        monkeypatch.setattr(registry, "resolve_symbol", fake_resolve_symbol)

        results = await asyncio.gather(*[
            registry.ticker("binance", "BTC/USDT", "spot")
            for _ in range(12)
        ])
        assert fake.fetch_count == 1
        assert all(result["last"] == pytest.approx(123.45) for result in results)

        # Returned objects are copies; a caller cannot corrupt the cache.
        results[0]["last"] = -1
        cached = await registry.ticker("binance", "BTC/USDT", "spot")
        assert cached["last"] == pytest.approx(123.45)
        assert fake.fetch_count == 1

        # Spot and Futures are distinct cache keys even for the same base pair.
        futures = await registry.ticker("binance", "BTC/USDT", "futures")
        assert futures["symbol"] == "BTC/USDT:USDT"
        assert fake.fetch_count == 2
        await registry.close()

    asyncio.run(run())


def test_ticker_cache_expires_and_fetches_fresh_price(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.fetch_count = 0

        async def fetch_ticker(self, symbol):
            self.fetch_count += 1
            return {"symbol": symbol, "last": float(self.fetch_count)}

    async def run():
        registry = ExchangeRegistry(ticker_cache_ttl=0.01)
        fake = FakeClient()

        async def fake_load_markets(exchange_id, market_type):
            return fake

        async def fake_resolve_symbol(exchange_id, symbol, market_type):
            return symbol

        monkeypatch.setattr(registry, "_load_markets", fake_load_markets)
        monkeypatch.setattr(registry, "resolve_symbol", fake_resolve_symbol)

        first = await registry.ticker("bybit", "BTC/USDT", "spot")
        second = await registry.ticker("bybit", "BTC/USDT", "spot")
        assert first["last"] == second["last"] == 1
        assert fake.fetch_count == 1

        await asyncio.sleep(0.02)
        fresh = await registry.ticker("bybit", "BTC/USDT", "spot")
        assert fresh["last"] == 2
        assert fake.fetch_count == 2
        await registry.close()

    asyncio.run(run())


def test_registry_rejects_invalid_ticker_cache_ttl():
    with pytest.raises(ValueError, match="ticker_cache_ttl"):
        ExchangeRegistry(ticker_cache_ttl=-1)



def test_rate_limit_429_sets_shared_cooldown_and_logs_retry_after(monkeypatch, caplog):
    import logging
    from backend.exchanges import ExchangeRateLimitCooldown

    class FakeRateLimitError(Exception):
        status_code = 429

    class FakeClient:
        last_response_headers = {"Retry-After": "30"}
        last_http_response = '{"msg":"Too many requests"}'
        def __init__(self):
            self.fetch_count = 0
        async def fetch_ticker(self, symbol):
            self.fetch_count += 1
            raise FakeRateLimitError("HTTP 429 Too Many Requests")

    async def run():
        registry = ExchangeRegistry()
        fake = FakeClient()

        async def fake_load_markets(exchange_id, market_type):
            return fake

        async def fake_resolve_symbol(exchange_id, symbol, market_type):
            return "BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT"

        monkeypatch.setattr(registry, "_load_markets", fake_load_markets)
        monkeypatch.setattr(registry, "resolve_symbol", fake_resolve_symbol)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ExchangeRateLimitCooldown) as first:
                await registry.ticker("binance", "BTC/USDT", "spot")
            assert first.value.status_code == 429
            assert first.value.retry_after >= 30

            # Cooldown applies to the other market-type client as well.
            with pytest.raises(ExchangeRateLimitCooldown) as second:
                await registry.ticker("binance", "BTC/USDT", "futures")
            assert second.value.status_code == 429
            assert fake.fetch_count == 1
            assert "Retry-After=30" in caplog.text
            assert "cooldown_seconds=" in caplog.text
        await registry.close()

    asyncio.run(run())


def test_rate_limit_418_uses_exchange_ban_timestamp_when_available(monkeypatch):
    import time
    from backend.exchanges import ExchangeRateLimitCooldown

    class FakeDDoSError(Exception):
        status_code = 418

    class FakeClient:
        last_response_headers = {}
        def __init__(self):
            self.fetch_count = 0
            self.last_http_response = ""
        async def fetch_ticker(self, symbol):
            self.fetch_count += 1
            self.last_http_response = (
                '{"msg":"Way too much request weight used; '
                f'IP banned until {int((time.time() + 600) * 1000)}."}}'
            )
            raise FakeDDoSError("HTTP 418 I'm a teapot")

    async def run():
        registry = ExchangeRegistry()
        fake = FakeClient()

        async def fake_load_markets(exchange_id, market_type):
            return fake

        async def fake_resolve_symbol(exchange_id, symbol, market_type):
            return symbol

        monkeypatch.setattr(registry, "_load_markets", fake_load_markets)
        monkeypatch.setattr(registry, "resolve_symbol", fake_resolve_symbol)

        with pytest.raises(ExchangeRateLimitCooldown) as error:
            await registry.ticker("binance", "BTC/USDT", "spot")
        assert error.value.status_code == 418
        assert error.value.retry_after >= 590
        assert fake.fetch_count == 1
        await registry.close()

    asyncio.run(run())


def test_guarded_shared_throttler_checks_cooldown_after_queue_wait(monkeypatch):
    import time
    from backend.exchanges import ExchangeRateLimitCooldown

    class BlockingThrottler:
        def __init__(self):
            self.release = asyncio.Event()
        async def __call__(self, cost=None):
            await self.release.wait()

    class FakeClient:
        def __init__(self, config):
            self.options = config["options"]
            self.base_throttler = BlockingThrottler()
            self.throttler = self.base_throttler
        async def close(self):
            pass

    monkeypatch.setattr("backend.exchanges.ccxt.binance", FakeClient)

    async def run():
        registry = ExchangeRegistry()
        spot = await registry.get("binance", "spot")
        futures = await registry.get("binance", "futures")
        assert spot.throttler is futures.throttler

        queued = asyncio.create_task(spot.throttler(1))
        await asyncio.sleep(0)
        registry._rate_limit_until["binance"] = time.monotonic() + 30
        registry._rate_limit_status["binance"] = 418
        spot.base_throttler.release.set()

        with pytest.raises(ExchangeRateLimitCooldown) as error:
            await queued
        assert error.value.status_code == 418
        assert error.value.retry_after >= 29
        await registry.close()

    asyncio.run(run())



def test_ticker_cache_stays_bounded_under_many_distinct_symbols(monkeypatch):
    from backend.exchanges import MAX_TICKER_CACHE_ENTRIES

    class FakeClient:
        async def fetch_ticker(self, symbol):
            return {"symbol": symbol, "last": 100.0}

    async def run():
        registry = ExchangeRegistry(ticker_cache_ttl=60)
        client = FakeClient()

        async def fake_load_markets(exchange_id, market_type):
            return client

        async def fake_resolve_symbol(exchange_id, symbol, market_type):
            return symbol

        monkeypatch.setattr(registry, "_load_markets", fake_load_markets)
        monkeypatch.setattr(registry, "resolve_symbol", fake_resolve_symbol)

        for index in range(MAX_TICKER_CACHE_ENTRIES + 300):
            await registry.ticker("kucoin", f"COIN{index}/USDT", "spot")

        assert len(registry._ticker_cache) <= MAX_TICKER_CACHE_ENTRIES
        assert registry._ticker_inflight == {}
        await registry.close()

    asyncio.run(run())


def test_kucoin_futures_uses_dedicated_ccxt_client(monkeypatch):
    from backend import exchanges as exchange_module

    class FakeSpotClient:
        def __init__(self, config):
            self.options = config["options"]
            self.throttler = object()
        async def close(self):
            pass

    class FakeFuturesClient:
        def __init__(self, config):
            self.options = config["options"]
            self.throttler = object()
        async def close(self):
            pass

    monkeypatch.setattr(exchange_module.ccxt, "kucoin", FakeSpotClient)
    monkeypatch.setattr(exchange_module.ccxt, "kucoinfutures", FakeFuturesClient, raising=False)

    async def run():
        registry = ExchangeRegistry()
        spot = await registry.get("kucoin", "spot")
        futures = await registry.get("kucoin", "futures")
        assert isinstance(spot, FakeSpotClient)
        assert isinstance(futures, FakeFuturesClient)
        assert spot.options["defaultType"] == "spot"
        assert futures.options["defaultType"] == "swap"
        assert ("kucoin", "spot") in registry._clients
        assert ("kucoin", "futures") in registry._clients
        await registry.close()

    asyncio.run(run())


def test_bybit_reset_timestamp_sets_shared_cooldown(monkeypatch):
    import time
    from backend.exchanges import ExchangeRateLimitCooldown

    class RateLimitExceeded(Exception):
        pass

    class FakeClient:
        last_response_headers = {
            "X-Bapi-Limit-Reset-Timestamp": str(int((time.time() + 45) * 1000))
        }
        last_http_response = '{"retCode":10006,"retMsg":"Too many visits!"}'

        def __init__(self):
            self.fetch_count = 0

        async def fetch_ticker(self, symbol):
            self.fetch_count += 1
            raise RateLimitExceeded("Bybit retCode 10006: Too many visits!")

    async def run():
        registry = ExchangeRegistry()
        fake = FakeClient()

        async def fake_load_markets(exchange_id, market_type):
            return fake

        async def fake_resolve_symbol(exchange_id, symbol, market_type):
            return "BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT"

        monkeypatch.setattr(registry, "_load_markets", fake_load_markets)
        monkeypatch.setattr(registry, "resolve_symbol", fake_resolve_symbol)

        with pytest.raises(ExchangeRateLimitCooldown) as first:
            await registry.ticker("bybit", "BTC/USDT", "spot")
        assert first.value.status_code == 429
        assert first.value.retry_after >= 40

        # Spot and Futures share the same venue cooldown: do not send a second
        # request while Bybit's advertised reset time is still in the future.
        with pytest.raises(ExchangeRateLimitCooldown) as second:
            await registry.ticker("bybit", "BTC/USDT", "futures")
        assert second.value.retry_after >= 39
        assert fake.fetch_count == 1
        await registry.close()


def test_bybit_reset_timestamp_ignores_stale_value():
    from backend.exchanges import ExchangeRegistry

    delay = ExchangeRegistry._bybit_reset_delay_from_headers({
        "x-bapi-limit-reset-timestamp": "1"
    })
    assert delay is None



def test_bybit_ip_rate_limit_403_closes_sessions_and_enforces_ten_minutes():
    from backend.exchanges import ExchangeRateLimitCooldown

    class AccessTooFrequent(Exception):
        status_code = 403

    class FakeClient:
        last_response_headers = {}
        last_http_response = '{"retCode":10018,"retMsg":"403, access too frequent"}'

        def __init__(self):
            self.close_count = 0

        async def close(self):
            self.close_count += 1

    async def run():
        registry = ExchangeRegistry()
        spot = FakeClient()
        futures = FakeClient()
        registry._clients[("bybit", "spot")] = spot
        registry._clients[("bybit", "futures")] = futures
        registry._shared_throttlers["bybit"] = object()
        registry._ticker_cache[("bybit", "spot", "BTC/USDT")] = (
            9999999999.0, {"last": 1.0}
        )

        async def forbidden_operation():
            raise AccessTooFrequent("403 Forbidden: access too frequent")

        with pytest.raises(ExchangeRateLimitCooldown) as error:
            await registry._request("bybit", spot, forbidden_operation)

        assert error.value.status_code == 403
        assert error.value.retry_after >= 600
        assert spot.close_count == 1
        assert futures.close_count == 1
        assert registry._clients == {}
        assert "bybit" not in registry._shared_throttlers
        assert not any(key[0] == "bybit" for key in registry._ticker_cache)

        # No new session may be opened until the 10-minute cooldown expires.
        with pytest.raises(ExchangeRateLimitCooldown) as blocked:
            await registry.get("bybit", "futures")
        assert blocked.value.status_code == 403
        assert registry._clients == {}
        await registry.close()

    asyncio.run(run())


def test_bybit_403_invalid_request_is_not_misclassified_as_ip_ban():
    from backend.exchanges import ExchangeRegistry

    class Forbidden(Exception):
        status_code = 403

    class FakeClient:
        last_http_response = '{"retMsg":"invalid request"}'

    assert not ExchangeRegistry._is_bybit_ip_rate_limit_error(
        FakeClient(), Forbidden("403 Forbidden: invalid request")
    )
