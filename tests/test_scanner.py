import asyncio
import json

from backend import scanner


def test_futures_scan_persists_canonical_symbol(monkeypatch):
    observed = {}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, sql, params):
            observed["sql"] = sql
            observed["params"] = params
            return type("Cursor", (), {"fetchone": lambda self: {"id": 42}})()

    async def fake_resolve_symbol(exchange, symbol, market_type):
        assert (exchange, symbol, market_type) == ("binance", "BTC/USDT", "futures")
        return "BTC/USDT:USDT"

    async def fake_ohlcv(exchange, symbol, timeframe, limit, market_type):
        observed["ohlcv_symbol"] = symbol
        assert symbol == "BTC/USDT:USDT"
        return [[1, 2, 3, 4, 5, 6], [2, 3, 4, 5, 6, 7]]

    metrics = {
        "signal": "LONG",
        "score": 80,
        "price": 60000.0,
        "rsi14": 55.0,
        "trend": "bullish",
    }

    monkeypatch.setattr(scanner.registry, "resolve_symbol", fake_resolve_symbol)
    monkeypatch.setattr(scanner.registry, "ohlcv", fake_ohlcv)
    monkeypatch.setattr(scanner, "analyze", lambda candles: metrics)
    monkeypatch.setattr(scanner, "connect", lambda: FakeConnection())
    monkeypatch.setattr(scanner, "archive_expired_signals", lambda: 0)

    results = asyncio.run(scanner.scan(
        "binance",
        symbols=["BTC/USDT"],
        market_type="futures",
        timeframe="1h",
    ))

    assert results[0]["symbol"] == "BTC/USDT:USDT"
    assert results[0]["signal_id"] == 42
    assert observed["ohlcv_symbol"] == "BTC/USDT:USDT"
    assert observed["params"][1] == "BTC/USDT:USDT"
    saved_payload = json.loads(observed["params"][-1])
    assert saved_payload["symbol"] == "BTC/USDT:USDT"

