import asyncio
import json

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from backend.config import Settings, settings
from backend import backtest, main
from backend.contracts import validate_instrument
from backend.exchanges import ExchangeRateLimitCooldown, ExchangeRegistry
from backend.risk import RiskManager


def _flat_candles(count=60):
    return [
        [index * 60_000, 100.0, 101.0, 99.0, 100.0, 1000.0]
        for index in range(count)
    ]


def _long_once(monkeypatch):
    calls = {"count": 0}

    def fake_analyze(_history):
        calls["count"] += 1
        return {
            "signal": "LONG" if calls["count"] == 1 else "WAIT",
            "price": 100.0,
            "atr14": 2.0,
        }

    monkeypatch.setattr(backtest, "analyze", fake_analyze)


def test_backtest_stop_gap_fills_at_adverse_open(monkeypatch):
    candles = _flat_candles()
    candles[51] = [51 * 60_000, 90.0, 95.0, 88.0, 91.0, 1000.0]
    _long_once(monkeypatch)

    result = backtest.run_backtest(candles)
    assert result["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "STOP_GAP"
    assert trade["exit"] == pytest.approx(90.0 * (1 - settings.slippage_bps / 10000))
    assert trade["exit"] < trade["stop"]


def test_backtest_target_gap_fills_at_favorable_open(monkeypatch):
    candles = _flat_candles()
    candles[51] = [51 * 60_000, 110.0, 112.0, 109.0, 111.0, 1000.0]
    _long_once(monkeypatch)

    result = backtest.run_backtest(candles)
    trade = result["trades"][0]
    assert trade["exit_reason"] == "TARGET_GAP"
    assert trade["exit"] == pytest.approx(110.0 * (1 - settings.slippage_bps / 10000))
    assert trade["exit"] > trade["target"]


def test_backtest_reports_unclosed_position_as_unrealized(monkeypatch):
    candles = _flat_candles()
    _long_once(monkeypatch)

    result = backtest.run_backtest(candles)
    assert result["trade_count"] == 0
    assert len(result["open_positions"]) == 1
    assert result["open_position"]["status"] == "OPEN"
    assert result["open_position"]["mark_price"] == pytest.approx(candles[-1][4])
    assert result["ending_balance"] == pytest.approx(result["starting_balance"])
    assert result["ending_equity"] == pytest.approx(
        result["ending_balance"] + result["unrealized_pnl"]
    )
    assert result["net_pnl"] == pytest.approx(result["unrealized_pnl"])
    assert [point["index"] for point in result["equity_curve"]].count(59) == 1
    json.dumps(result, allow_nan=False)


def test_backtest_rejects_inconsistent_ohlc():
    candles = _flat_candles()
    candles[10] = [10 * 60_000, 100.0, 98.0, 99.0, 100.0, 1000.0]
    with pytest.raises(ValueError, match="inconsistent high/low"):
        backtest.run_backtest(candles)


def test_settings_reject_inconsistent_risk_relationships():
    with pytest.raises(ValidationError, match="risk_per_trade_pct"):
        Settings(
            _env_file=None,
            risk_per_trade_pct=3.0,
            max_open_risk_pct=2.0,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("taker_fee_rate", -0.001),
        ("taker_fee_rate", 1.0),
        ("slippage_bps", -1.0),
        ("slippage_bps", 10000.0),
    ],
)
def test_settings_reject_invalid_cost_configuration(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_live_risk_engine_and_backtest_share_cost_validation(monkeypatch):
    monkeypatch.setattr(settings, "taker_fee_rate", -0.01)
    decision = RiskManager().size(10000, 100, 95)
    assert not decision.approved
    assert "taker_fee_rate" in decision.reason
    with pytest.raises(ValueError, match="taker_fee_rate"):
        backtest.run_backtest(_flat_candles())


def test_spot_risk_sizing_applies_exchange_amount_precision_and_limits():
    decision = RiskManager().size(
        10000, 100, 95,
        instrument={
            "contract_size": 1.0,
            "contract_type": "spot",
            "amount_precision": 2,
            "precision_mode": 2,
            "min_amount": 0.01,
            "max_amount": 1.239,
            "min_cost": 5,
            "max_cost": 2000,
        },
    )
    assert decision.approved
    assert decision.quantity == pytest.approx(1.23)
    assert decision.quantity <= 1.239
    assert decision.notional <= 2000


def test_spot_instrument_validation_requires_and_returns_precision():
    spec = validate_instrument(
        "spot",
        {
            "spot": True,
            "type": "spot",
            "precision": {"amount": 4},
            "limits": {"amount": {"min": 0.0001, "max": 100}},
            "base": "BTC",
            "quote": "USDT",
        },
    )
    assert spec["contract_type"] == "spot"
    assert spec["amount_precision"] == 4
    assert spec["min_amount"] == pytest.approx(0.0001)


def test_exchange_registry_coalesces_concurrent_market_downloads(monkeypatch):
    class FakeThrottler:
        async def __call__(self, cost=None):
            await asyncio.sleep(0)

    class FakeClient:
        loads = 0

        def __init__(self, config):
            self.options = config["options"]
            self.throttler = FakeThrottler()
            self.markets = {}

        async def load_markets(self):
            type(self).loads += 1
            await asyncio.sleep(0.02)
            self.markets = {"BTC/USDT": {"symbol": "BTC/USDT"}}
            return self.markets

        async def close(self):
            return None

    FakeClient.loads = 0
    monkeypatch.setattr("backend.exchanges.ccxt.binance", FakeClient)

    async def run():
        registry = ExchangeRegistry()
        clients = await asyncio.gather(*[
            registry._load_markets("binance", "spot") for _ in range(12)
        ])
        assert FakeClient.loads == 1
        assert all(client is clients[0] for client in clients)
        await registry.close()

    asyncio.run(run())


def test_rate_limit_api_response_preserves_retry_after(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    async def fake_resolve_symbol(exchange, symbol, market_type):
        return symbol

    async def fake_ticker(exchange, symbol, market_type):
        raise ExchangeRateLimitCooldown(exchange, 429, 17)

    monkeypatch.setattr(main.registry, "resolve_symbol", fake_resolve_symbol)
    monkeypatch.setattr(main.registry, "ticker", fake_ticker)

    with TestClient(main.app) as client:
        response = client.get("/api/markets/bybit/BTC/USDT?market_type=spot")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert response.headers["X-Exchange-RateLimit-Status"] == "429"
    assert response.json()["upstream_status"] == 429
    assert response.json()["retry_after_seconds"] == 17


def test_dedicated_monitor_worker_initializes_and_closes(monkeypatch):
    from backend import monitor_worker

    called = {"init": False, "monitor": False, "closed": False}

    def fake_init_db():
        called["init"] = True

    async def fake_monitor(stop_event):
        called["monitor"] = True
        stop_event.set()

    async def fake_close():
        called["closed"] = True

    monkeypatch.setattr(monitor_worker, "init_db", fake_init_db)
    monkeypatch.setattr(monitor_worker, "run_position_monitor", fake_monitor)
    monkeypatch.setattr(monitor_worker.registry, "close", fake_close)

    asyncio.run(monitor_worker.serve())
    assert called == {"init": True, "monitor": True, "closed": True}
