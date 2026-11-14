import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.database import connect, init_db
from backend.main import app
from backend.paper import open_trade, close_trade, account
from backend.risk import risk_manager


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'paper.db'}")
    init_db()
    yield


def test_spot_short_is_rejected(isolated_db):
    with pytest.raises(ValueError, match="SHORT.*spot"):
        open_trade("binance", "BTC/USDT", "spot", "SHORT", 100, 105)


def test_open_and_close_spot_long(isolated_db):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95, 110)
    assert opened["status"] == "OPEN"
    closed = close_trade(opened["id"], 105)
    assert closed["status"] == "CLOSED"
    assert closed["pnl"] > 0
    assert account()["realized_pnl"] == pytest.approx(closed["pnl"])


def test_close_trade_cannot_be_repeated(isolated_db):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    close_trade(opened["id"], 101)
    with pytest.raises(ValueError, match="not found"):
        close_trade(opened["id"], 102)


def test_daily_realized_loss_blocks_new_trade(isolated_db, monkeypatch):
    monkeypatch.setattr(risk_manager, "max_daily_loss_pct", 3.0)
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    close_trade(opened["id"], 60)
    with pytest.raises(ValueError, match="Daily loss limit"):
        open_trade("binance", "ETH/USDT", "spot", "LONG", 100, 95)


def test_futures_short_is_allowed_in_paper_mode(isolated_db):
    opened = open_trade("bybit", "BTC/USDT:USDT", "futures", "SHORT", 100, 105, instrument={"contract_size": 0.001, "contract_type": "linear", "amount_precision": 3, "precision_mode": 2, "min_amount": 0.001})
    assert opened["status"] == "OPEN"


def test_api_rejects_spot_short(isolated_db):
    with TestClient(app, headers={"Authorization": "Basic dGVzdC10cmFkZXI6dGVzdC1vbmx5LXBhc3N3b3Jk"}) as client:
        response = client.post("/api/paper/open", json={
            "exchange": "binance", "symbol": "BTC/USDT", "market_type": "spot",
            "side": "SHORT", "entry": 100, "stop": 105
        })
    assert response.status_code == 422


def test_missing_live_prices_block_new_trade(isolated_db):
    with pytest.raises(ValueError, match="live price unavailable"):
        open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95,
                   prices_complete=False)


def test_duplicate_open_symbol_is_rejected(isolated_db):
    open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    with pytest.raises(ValueError, match="already exists"):
        open_trade("BINANCE", "btc/usdt", "spot", "LONG", 100, 95)


def test_default_target_is_two_to_one(isolated_db):
    opened = open_trade("binance", "ETH/USDT", "spot", "LONG", 100, 95)
    assert opened["target"] == pytest.approx(110)
    short = open_trade("bybit", "ETH/USDT:USDT", "futures", "SHORT", 100, 105, instrument={"contract_size": 0.001, "contract_type": "linear", "amount_precision": 3, "precision_mode": 2, "min_amount": 0.001})
    assert short["target"] == pytest.approx(90)


def test_floating_daily_loss_blocks_new_trade(isolated_db):
    with pytest.raises(ValueError, match="Daily loss limit"):
        open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95,
                   unrealized_pnl=-300)



def test_open_trade_reports_actual_risk_after_notional_cap(isolated_db):
    opened = open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 99)
    assert opened["risk_pct_actual"] < settings.risk_per_trade_pct
    assert opened["sizing_reason"] == "position_notional_cap"
    assert opened["fees_estimated"] > 0


def test_closed_trade_records_net_pnl_and_r_multiple(isolated_db):
    opened = open_trade("binance", "ETH/USDT", "spot", "LONG", 100, 95, 110)
    closed = close_trade(opened["id"], 110)
    assert closed["costs"] > 0
    assert closed["gross_pnl"] > closed["pnl"]
    assert closed["r_multiple"] > 0


def test_maximum_three_open_positions(isolated_db):
    open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    open_trade("binance", "ETH/USDT", "spot", "LONG", 100, 95)
    open_trade("binance", "SOL/USDT", "spot", "LONG", 100, 95)
    with pytest.raises(ValueError, match="Maximum open paper positions"):
        open_trade("binance", "BNB/USDT", "spot", "LONG", 100, 95)


def test_futures_inverse_sizing_uses_quote_face_value(isolated_db):
    opened = open_trade(
        "okx", "BTC/USD:BTC", "futures", "SHORT", 100, 105,
        instrument={
            "contract_size": 100, "contract_type": "inverse",
            "amount_precision": 1, "precision_mode": 2, "min_amount": 1,
        },
    )
    assert opened["status"] == "OPEN"
    assert opened["contracts"] == opened["quantity"]
    assert opened["quantity"] == round(opened["quantity"], 1)
    assert opened["notional"] == pytest.approx(opened["quantity"] * 100)


def test_api_assigns_and_persists_default_two_r_target(isolated_db, monkeypatch):
    from backend.exchanges import registry

    async def fake_market_spec(exchange, symbol, market_type):
        return {
            "symbol": symbol,
            "contract_size": 1.0,
            "contract_type": "spot",
            "amount_precision": 8,
            "precision_mode": 2,
            "min_amount": 0.00000001,
        }

    async def fake_ticker(exchange, symbol, market_type):
        return {"last": 100.0}

    async def fake_positions():
        return {"positions": []}

    monkeypatch.setattr(registry, "market_spec", fake_market_spec)
    monkeypatch.setattr(registry, "ticker", fake_ticker)
    monkeypatch.setattr("backend.main.get_positions", fake_positions)

    with TestClient(app, headers={"Authorization": "Basic dGVzdC10cmFkZXI6dGVzdC1vbmx5LXBhc3N3b3Jk"}) as client:
        response = client.post("/api/paper/open", json={
            "exchange": "kucoin",
            "symbol": "BTC/USDT",
            "market_type": "spot",
            "side": "LONG",
            "entry": 100,
            "stop": 95,
        })

    assert response.status_code == 200
    payload = response.json()
    assert payload["target"] == pytest.approx(110)
    assert payload["reward_risk_ratio"] == pytest.approx(2.0)

    with connect() as db:
        row = db.execute(
            "SELECT target FROM trades WHERE id=?", (payload["id"],)
        ).fetchone()
    assert row["target"] == pytest.approx(110)



def test_default_target_uses_configured_reward_risk_ratio(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "reward_risk_ratio", 3.0)
    opened = open_trade("binance", "ADA/USDT", "spot", "LONG", 100, 95)
    assert opened["target"] == pytest.approx(115)
    assert opened["reward_risk_ratio"] == pytest.approx(3.0)
    assert opened["default_reward_risk_ratio"] == pytest.approx(3.0)


def test_open_position_limit_uses_settings(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "max_open_trades", 2)
    open_trade("binance", "BTC/USDT", "spot", "LONG", 100, 95)
    open_trade("binance", "ETH/USDT", "spot", "LONG", 100, 95)
    with pytest.raises(ValueError, match=r"\(2\)"):
        open_trade("binance", "SOL/USDT", "spot", "LONG", 100, 95)



def test_paper_open_schema_uses_configured_default_exchange():
    from backend.main import PaperOpen

    payload = PaperOpen(
        symbol="BTC/USDT",
        side="LONG",
        entry=100,
        stop=95,
    )
    assert payload.exchange == settings.default_exchange


def test_exchange_metadata_exposes_configured_default():
    from backend.main import exchanges

    payload = exchanges()
    assert payload["exchanges"] == ["kucoin", "bybit", "okx"]
    assert payload["default_exchange"] == settings.default_exchange


def _linear_futures_spec():
    return {
        "contract_size": 0.001,
        "contract_type": "linear",
        "amount_precision": 3,
        "precision_mode": 2,
        "min_amount": 0.001,
        "max_amount": 100000,
        "market_type": "futures",
    }


def test_futures_open_persists_isolated_margin_liquidation_and_funding_state(isolated_db):
    opened = open_trade(
        "bybit", "BTC/USDT:USDT", "futures", "LONG", 100, 95, 120,
        instrument=_linear_futures_spec(),
        leverage=5,
        funding_rate=0.001,
        funding_interval_hours=8,
        maintenance_margin_rate=0.005,
        liquidation_fee_rate=0.005,
    )

    assert opened["status"] == "OPEN"
    assert opened["leverage"] == pytest.approx(5)
    assert opened["initial_margin"] == pytest.approx(opened["notional"] / 5)
    assert 0 < opened["liquidation_price"] < 100
    assert opened["funding_rate"] == pytest.approx(0.001)
    assert opened["funding_interval_hours"] == pytest.approx(8)

    with connect() as db:
        row = db.execute(
            "SELECT leverage, initial_margin, maintenance_margin_rate, "
            "liquidation_fee_rate, liquidation_price, funding_rate, "
            "funding_interval_hours, funding_paid, funding_events, next_funding_at "
            "FROM trades WHERE id=?",
            (opened["id"],),
        ).fetchone()
    assert row["leverage"] == pytest.approx(5)
    assert row["initial_margin"] == pytest.approx(opened["initial_margin"])
    assert row["liquidation_price"] == pytest.approx(opened["liquidation_price"])
    assert row["funding_paid"] == pytest.approx(0)
    assert row["funding_events"] == 0
    assert row["next_funding_at"] is not None


def test_due_futures_funding_is_persisted_once_and_included_in_close_pnl(isolated_db):
    from backend.paper import apply_due_funding
    from backend.metrics import position_pnl

    opened = open_trade(
        "bybit", "BTC/USDT:USDT", "futures", "LONG", 100, 95, 120,
        instrument=_linear_futures_spec(), leverage=5,
        funding_rate=0.001, funding_interval_hours=8,
    )
    notional = opened["notional"]
    due_at = opened["next_funding_at"] + 1
    updated = apply_due_funding(opened["id"], 100, now=due_at)
    assert updated["funding_events"] == 1
    assert updated["funding_paid"] == pytest.approx(notional * 0.001)

    repeated = apply_due_funding(opened["id"], 100, now=due_at)
    assert repeated["funding_events"] == 1
    assert repeated["funding_paid"] == pytest.approx(notional * 0.001)

    expected = position_pnl(
        100, opened["quantity"], "LONG", 101,
        0.001, "linear", funding_paid=notional * 0.001,
    )
    closed = close_trade(opened["id"], 101, observed_at=due_at)
    assert closed["funding_events"] == 1
    assert closed["funding_paid"] == pytest.approx(notional * 0.001)
    assert closed["pnl"] == pytest.approx(expected["unrealized_pnl"])


def test_futures_close_records_liquidation_instead_of_manual_exit_when_threshold_crossed(isolated_db):
    opened = open_trade(
        "okx", "BTC/USDT:USDT", "futures", "LONG", 100, 95, 120,
        instrument=_linear_futures_spec(), leverage=5,
        funding_rate=0.0,
    )
    exit_price = opened["liquidation_price"] - 1.0
    closed = close_trade(opened["id"], exit_price)

    assert closed["exit_reason"] == "LIQUIDATION"
    assert closed["liquidation_fee"] > 0
    assert closed["pnl"] < 0
    with connect() as db:
        row = db.execute(
            "SELECT status, exit_reason, exit_price FROM trades WHERE id=?",
            (opened["id"],),
        ).fetchone()
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "LIQUIDATION"
    assert row["exit_price"] == pytest.approx(exit_price)


def test_futures_margin_cap_rejects_leverage_above_configured_max(isolated_db):
    with pytest.raises(ValueError, match="leverage must be between"):
        open_trade(
            "bybit", "BTC/USDT:USDT", "futures", "LONG", 100, 95,
            instrument=_linear_futures_spec(), leverage=6,
        )
