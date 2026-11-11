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
        def market(self, symbol):
            assert symbol == "BTC/USDT:USDT"
            return {
                "symbol": symbol, "contract": True, "swap": True,
                "linear": True, "inverse": False, "contractSize": 0.001,
                "precision": {"amount": 3},
                "limits": {"amount": {"min": 0.001, "max": 100000}},
                "base": "BTC", "quote": "USDT", "settle": "USDT",
            }

    async def run():
        registry = ExchangeRegistry()
        fake = FakeClient()
        async def fake_get(exchange_id, market_type):
            return fake
        monkeypatch.setattr(registry, "get", fake_get)
        spec = await registry.market_spec("bybit", "BTC/USDT:USDT", "futures")
        assert spec["contract_type"] == "linear"
        assert spec["contract_size"] == pytest.approx(0.001)
        assert spec["precision_mode"] == 2
        assert spec["min_amount"] == pytest.approx(0.001)
    asyncio.run(run())
