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
