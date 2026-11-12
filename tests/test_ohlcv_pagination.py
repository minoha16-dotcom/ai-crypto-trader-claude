import asyncio

import pytest

from backend.exchanges import ExchangeRateLimitCooldown, ExchangeRegistry


class FakeClient:
    def parse_timeframe(self, timeframe):
        return 60

    async def fetch_ohlcv(self, *args, **kwargs):
        raise AssertionError("_request fake should intercept fetch_ohlcv")


def _candle(timestamp):
    return [timestamp, 100, 101, 99, 100, 10]


def test_ohlcv_paginates_short_pages_and_respects_venue_max(monkeypatch):
    registry = ExchangeRegistry()
    now = 10_000_000
    monkeypatch.setattr("backend.exchanges.time.time", lambda: now / 1000)
    monkeypatch.setattr("backend.exchanges.OHLCV_REQUEST_MAX", {("okx", "spot"): 2})
    monkeypatch.setattr(registry, "_load_markets", lambda *_: asyncio.sleep(0, result=FakeClient()))
    monkeypatch.setattr(registry, "resolve_symbol", lambda *args: asyncio.sleep(0, result=args[1]))
    calls = []

    async def request(exchange, client, operation, symbol, *, timeframe, since, limit):
        calls.append((since, limit))
        index = len(calls) - 1
        return [_candle(since + step * 60_000) for step in range(2)]

    monkeypatch.setattr(registry, "_request", request)
    candles, metadata = asyncio.run(registry.ohlcv_with_metadata("okx", "BTC/USDT", "1m", 5))
    assert len(candles) == 5
    assert len(calls) == 3
    assert all(limit == 2 for _, limit in calls)
    assert metadata["requested"] == 5
    assert metadata["received"] == 5
    assert metadata["shortfall"] == 0


def test_ohlcv_deduplicates_and_sorts_out_of_order_pages(monkeypatch):
    registry = ExchangeRegistry()
    now = 10_000_000
    monkeypatch.setattr("backend.exchanges.time.time", lambda: now / 1000)
    monkeypatch.setattr("backend.exchanges.OHLCV_REQUEST_MAX", {("okx", "spot"): 2})
    monkeypatch.setattr(registry, "_load_markets", lambda *_: asyncio.sleep(0, result=FakeClient()))
    monkeypatch.setattr(registry, "resolve_symbol", lambda *args: asyncio.sleep(0, result=args[1]))
    pages = [[_candle(9_820_000), _candle(9_760_000)],
             [_candle(9_820_000), _candle(9_880_000)],
             [_candle(9_940_000)]]

    async def request(*args, **kwargs):
        return pages.pop(0)

    monkeypatch.setattr(registry, "_request", request)
    candles, metadata = asyncio.run(registry.ohlcv_with_metadata("okx", "BTC/USDT", "1m", 4))
    assert [c[0] for c in candles] == [9_760_000, 9_820_000, 9_880_000, 9_940_000]
    assert metadata["received"] == 4
    assert metadata["gaps"] == []


def test_ohlcv_reports_timestamp_gaps_and_shortfall(monkeypatch):
    registry = ExchangeRegistry()
    monkeypatch.setattr("backend.exchanges.time.time", lambda: 10_000)
    monkeypatch.setattr(registry, "_load_markets", lambda *_: asyncio.sleep(0, result=FakeClient()))
    monkeypatch.setattr(registry, "resolve_symbol", lambda *args: asyncio.sleep(0, result=args[1]))

    async def request(*args, **kwargs):
        return [_candle(9_760_000), _candle(9_880_000)]

    monkeypatch.setattr(registry, "_request", request)
    candles, metadata = asyncio.run(registry.ohlcv_with_metadata("okx", "BTC/USDT", "1m", 4))
    assert len(candles) == 2
    assert metadata["shortfall"] == 2
    assert metadata["continuity"] == "gaps_detected"
    assert metadata["gaps"][0]["missing"] == 1


def test_ohlcv_pagination_is_bounded_when_provider_repeats_page(monkeypatch):
    registry = ExchangeRegistry()
    monkeypatch.setattr("backend.exchanges.time.time", lambda: 10_000)
    monkeypatch.setattr("backend.exchanges.OHLCV_REQUEST_MAX", {("okx", "spot"): 1})
    monkeypatch.setattr(registry, "_load_markets", lambda *_: asyncio.sleep(0, result=FakeClient()))
    monkeypatch.setattr(registry, "resolve_symbol", lambda *args: asyncio.sleep(0, result=args[1]))
    calls = []

    async def request(*args, **kwargs):
        calls.append(1)
        return [_candle(9_400_000)]

    monkeypatch.setattr(registry, "_request", request)
    _, metadata = asyncio.run(registry.ohlcv_with_metadata("okx", "BTC/USDT", "1m", 10))
    assert len(calls) <= 12
    assert metadata["request_count"] == len(calls)


def test_ohlcv_propagates_exchange_cooldown(monkeypatch):
    registry = ExchangeRegistry()
    monkeypatch.setattr(registry, "_load_markets", lambda *_: asyncio.sleep(0, result=FakeClient()))
    monkeypatch.setattr(registry, "resolve_symbol", lambda *args: asyncio.sleep(0, result=args[1]))

    async def request(*args, **kwargs):
        raise ExchangeRateLimitCooldown("okx", 429, 60)

    monkeypatch.setattr(registry, "_request", request)
    with pytest.raises(ExchangeRateLimitCooldown):
        asyncio.run(registry.ohlcv_with_metadata("okx", "BTC/USDT", "1m", 10))
