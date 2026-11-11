import pytest

from scripts.check_exchanges import is_region_restriction


@pytest.mark.parametrize(
    "message",
    [
        "ExchangeNotAvailable: HTTP 451 restricted location",
        "403 Forbidden: The Amazon CloudFront distribution is configured to block access from your country",
    ],
)
def test_exchange_smoke_classifies_explicit_geo_blocks_as_skips(message):
    assert is_region_restriction(message)


@pytest.mark.parametrize(
    "message",
    [
        "RequestTimeout: network timeout",
        "RateLimitExceeded: too many requests",
        "403 Forbidden: invalid request",
    ],
)
def test_exchange_smoke_does_not_hide_non_geographic_failures(message):
    assert not is_region_restriction(message)



def test_exchange_smoke_runs_sequentially_and_fails_on_rate_limit(monkeypatch, capsys):
    import asyncio
    from scripts import check_exchanges
    from backend.exchanges import ExchangeRateLimitCooldown

    monkeypatch.setenv("SMOKE_REQUEST_PAUSE_SECONDS", "0")
    calls = []

    class FakeRegistry:
        async def close(self):
            return None

    async def fake_check(registry, exchange, market_type):
        calls.append((exchange, market_type))
        if exchange == "bybit":
            raise ExchangeRateLimitCooldown("bybit", 429, 30)
        return {
            "exchange": exchange,
            "market_type": market_type,
            "symbol": "BTC/USDT",
            "last": 100.0,
            "contract_type": "spot" if market_type == "spot" else "linear",
            "contract_size": 1.0,
            "amount_precision": 3,
        }

    monkeypatch.setattr(check_exchanges, "ExchangeRegistry", FakeRegistry)
    monkeypatch.setattr(check_exchanges, "check", fake_check)

    with pytest.raises(SystemExit) as error:
        asyncio.run(check_exchanges.main())

    assert error.value.code == 1
    assert calls == [
        ("binance", "spot"), ("binance", "futures"),
        ("bybit", "spot"), ("bybit", "futures"),
        ("okx", "spot"), ("okx", "futures"),
    ]
    output = capsys.readouterr().out
    assert "COOLDOWN bybit spot" in output
    assert "SUMMARY passed=4" in output
    assert "cooldown_blocked=2" in output
