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
        ("GET", "/api/instruments/kucoin/BTC/USDT", {"market_type": "spot"}),
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



def test_mcp_backtest_tool_forwards_volume_ratio_threshold(monkeypatch):
    captured = {}

    async def fake_api(method, path, *, params=None, payload=None):
        captured.update(method=method, path=path, params=params)
        return {"trade_count": 0}

    monkeypatch.setattr(server, "_api", fake_api)
    result = asyncio.run(server.run_backtest(
        "okx", "BTC/USDT", "spot", "1h", 200, 0.8
    ))
    assert result["trade_count"] == 0
    assert captured["params"]["volume_ratio_threshold"] == 0.8


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1])
def test_mcp_backtest_rejects_invalid_volume_ratio_threshold(value):
    with pytest.raises(ValueError, match="volume_ratio_threshold"):
        asyncio.run(server.run_backtest("okx", "BTC/USDT", volume_ratio_threshold=value))



def test_mcp_scan_uses_backend_default_exchange_when_omitted(monkeypatch):
    calls = []

    async def fake_api(method, path, *, params=None, payload=None):
        calls.append((method, path, params))
        if path == "/api/exchanges":
            return {
                "exchanges": ["kucoin", "bybit", "okx"],
                "default_exchange": "bybit",
            }
        return {"results": []}

    monkeypatch.setattr(server, "_api", fake_api)
    result = asyncio.run(server.scan_opportunities(symbols="BTC/USDT"))

    assert result == {"results": []}
    assert calls == [
        ("GET", "/api/exchanges", None),
        ("POST", "/api/scan", {
            "exchange": "bybit",
            "market_type": "spot",
            "symbols": "BTC/USDT",
            "timeframe": "1h",
        }),
    ]


def test_mcp_scan_rejects_unsupported_backend_default(monkeypatch):
    async def fake_api(method, path, *, params=None, payload=None):
        return {"exchanges": ["kucoin", "bybit", "okx"], "default_exchange": "binance"}

    monkeypatch.setattr(server, "_api", fake_api)
    with pytest.raises(ValueError, match="supported default exchange"):
        asyncio.run(server.scan_opportunities())
