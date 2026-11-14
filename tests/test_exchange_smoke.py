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



def test_exchange_smoke_reports_provider_rate_limits_as_inconclusive(monkeypatch, capsys):
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
        if exchange == "bybit" and market_type == "spot":
            raise ExchangeRateLimitCooldown("bybit", 429, 0)
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

    result = asyncio.run(check_exchanges.main())

    assert result == 2
    assert calls == [
        ("kucoin", "spot"), ("kucoin", "futures"),
        ("bybit", "spot"),
        ("okx", "spot"), ("okx", "futures"),
    ]
    output = capsys.readouterr().out
    assert "COOLDOWN bybit spot" in output
    assert "INCONCLUSIVE bybit futures: skipped after" in output
    assert "SUMMARY passed=4" in output
    assert "cooldown_blocked=1" in output
    assert "INCONCLUSIVE: 2 checks" in output



def _run_smoke_with_fake_registry(monkeypatch, fake_check):
    import asyncio
    from scripts import check_exchanges

    class FakeRegistry:
        async def close(self):
            return None

    monkeypatch.setattr(check_exchanges, "ExchangeRegistry", FakeRegistry)
    monkeypatch.setattr(check_exchanges, "check", fake_check)
    monkeypatch.setenv("SMOKE_REQUEST_PAUSE_SECONDS", "0")
    return asyncio.run(check_exchanges.main())


def test_exchange_smoke_returns_zero_only_when_all_six_checks_pass(monkeypatch):
    async def fake_check(registry, exchange, market_type):
        return {
            "exchange": exchange,
            "market_type": market_type,
            "symbol": "BTC/USDT" if market_type == "spot" else "BTC/USDT:USDT",
            "last": 100.0,
            "contract_type": "spot" if market_type == "spot" else "linear",
            "contract_size": 1.0,
            "amount_precision": 3,
            "precision_mode": 2,
        }

    assert _run_smoke_with_fake_registry(monkeypatch, fake_check) == 0


def test_exchange_smoke_returns_one_for_a_real_endpoint_failure(monkeypatch):
    async def fake_check(registry, exchange, market_type):
        if exchange == "okx" and market_type == "futures":
            raise RuntimeError("invalid contract metadata")
        return {
            "exchange": exchange,
            "market_type": market_type,
            "symbol": "BTC/USDT",
            "last": 100.0,
            "contract_type": "spot" if market_type == "spot" else "linear",
            "contract_size": 1.0,
            "amount_precision": 3,
            "precision_mode": 2,
        }

    assert _run_smoke_with_fake_registry(monkeypatch, fake_check) == 1


def test_exchange_smoke_does_not_treat_all_geo_blocks_as_success(monkeypatch):
    async def fake_check(registry, exchange, market_type):
        raise RuntimeError("HTTP 451 restricted location")

    assert _run_smoke_with_fake_registry(monkeypatch, fake_check) == 2
