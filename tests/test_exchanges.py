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
        fake = FakeClient()
        async def fake_get(exchange_id, market_type):
            return fake
        registry.get = fake_get
        with pytest.raises(ValueError, match="ambiguous"):
            await registry.resolve_symbol("okx", "BTC/USD", "futures")
        assert await registry.resolve_symbol("okx", "BTC/USD:USDT", "futures") == "BTC/USD:USDT"
        await registry.close()
    asyncio.run(run())
