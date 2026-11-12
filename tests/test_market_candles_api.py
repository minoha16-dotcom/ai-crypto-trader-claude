import asyncio

import pytest
from fastapi import HTTPException

from backend import main


def test_market_candles_returns_validated_public_ohlcv(monkeypatch):
    calls = {}

    async def fake_resolve_symbol(exchange, symbol, market_type):
        calls["resolved"] = (exchange, symbol, market_type)
        return "BTC/USDT:USDT" if market_type == "futures" else "BTC/USDT"

    async def fake_ohlcv(exchange, symbol, timeframe, limit, market_type):
        calls["ohlcv"] = (exchange, symbol, timeframe, limit, market_type)
        return [
            [1_700_000_000_000, 100, 110, 95, 105, 12.5],
            [1_700_000_060_000, 105, 108, 101, 103, 8],
            [1_700_000_120_000, 103, 104, 102, 0, 4],
            [1_700_000_180_000, 103, float("nan"), 100, 102, 3],
            [1_700_000_240_000, 103, 101, 104, 102, 3],
        ]

    monkeypatch.setattr(main.registry, "resolve_symbol", fake_resolve_symbol)
    monkeypatch.setattr(main.registry, "ohlcv", fake_ohlcv)

    result = asyncio.run(
        main.market_candles(
            "bybit", "BTC/USDT", market_type="futures", timeframe="15m", limit=80
        )
    )

    assert result["exchange"] == "bybit"
    assert result["symbol"] == "BTC/USDT:USDT"
    assert result["market_type"] == "futures"
    assert result["timeframe"] == "15m"
    assert len(result["candles"]) == 2
    assert result["candles"][0] == {
        "timestamp": 1_700_000_000_000,
        "open": 100.0,
        "high": 110.0,
        "low": 95.0,
        "close": 105.0,
        "volume": 12.5,
    }
    assert calls["resolved"] == ("bybit", "BTC/USDT", "futures")
    assert calls["ohlcv"] == ("bybit", "BTC/USDT:USDT", "15m", 80, "futures")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"exchange": "not-an-exchange", "symbol": "BTC/USDT"}, "Unsupported exchange"),
        ({"exchange": "kucoin", "symbol": "BTC/USDT", "market_type": "margin"}, "market_type"),
        ({"exchange": "kucoin", "symbol": "BTC/USDT", "timeframe": "17m"}, "timeframe"),
        ({"exchange": "kucoin", "symbol": "BTC/USDT", "limit": 10}, "between 20 and 200"),
        ({"exchange": "kucoin", "symbol": "BTC/USDT", "limit": 201}, "between 20 and 200"),
    ],
)
def test_market_candles_rejects_invalid_parameters(kwargs, message):
    params = {
        "exchange": "kucoin",
        "symbol": "BTC/USDT",
        "market_type": "spot",
        "timeframe": "1h",
        "limit": 80,
    }
    params.update(kwargs)

    with pytest.raises(HTTPException, match=message):
        asyncio.run(main.market_candles(**params))
