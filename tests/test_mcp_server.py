import asyncio

import pytest

from mcp_server import server


def test_mcp_backtest_tool_routes_to_backtest_api(monkeypatch):
    captured = {}

    async def fake_api(method, path, *, params=None, payload=None):
        captured.update(method=method, path=path, params=params, payload=payload)
        return {"trade_count": 0, "open_positions": []}

    monkeypatch.setattr(server, "_api", fake_api)
    result = asyncio.run(server.run_backtest(
        "okx", "btc/usdt", "futures", "4h", 500
    ))
    assert result["trade_count"] == 0
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/backtest"
    assert captured["params"] == {
        "exchange": "okx",
        "symbol": "BTC/USDT",
        "market_type": "futures",
        "timeframe": "4h",
        "limit": 500,
    }


def test_mcp_positions_opportunities_and_instrument_tools(monkeypatch):
    calls = []

    async def fake_api(method, path, *, params=None, payload=None):
        calls.append((method, path, params))
        return {"ok": True}

    monkeypatch.setattr(server, "_api", fake_api)
    asyncio.run(server.get_open_positions())
    asyncio.run(server.get_opportunities(5))
    asyncio.run(server.get_instrument_spec("kucoin", "btc/usdt", "spot"))
    assert calls == [
        ("GET", "/api/positions", None),
        ("GET", "/api/opportunities", {"limit": 5}),
        ("GET", "/api/instruments/binance/BTC/USDT", {"market_type": "spot"}),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exchange": "kucoin", "symbol": "BTCUSDT", "limit": 100},
        {"exchange": "kucoin", "symbol": "BTC/USDT", "limit": 52},
        {"exchange": "kucoin", "symbol": "BTC/USDT", "limit": 5001},
    ],
)
def test_mcp_backtest_tool_validates_symbol_and_limit(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(server.run_backtest(**kwargs))


def test_mcp_opportunities_tool_validates_limit():
    with pytest.raises(ValueError):
        asyncio.run(server.get_opportunities(31))
