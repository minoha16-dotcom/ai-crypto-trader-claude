import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import main
from backend.main import PaperClose, PaperOpen


def valid_trade(**overrides):
    payload = {
        "exchange": "binance",
        "symbol": "BTC/USDT",
        "market_type": "spot",
        "side": "LONG",
        "entry": 60000,
        "stop": 59000,
        "target": 62000,
    }
    payload.update(overrides)
    return payload


def test_paper_open_accepts_valid_spot_long():
    trade = PaperOpen(**valid_trade())
    assert trade.symbol == "BTC/USDT"
    assert trade.side == "LONG"


@pytest.mark.parametrize("field", ["entry", "stop", "target"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_paper_open_rejects_non_finite_prices(field, value):
    with pytest.raises(ValidationError):
        PaperOpen(**valid_trade(**{field: value}))


def test_paper_open_rejects_short_spot():
    with pytest.raises(ValidationError, match="SHORT paper positions"):
        PaperOpen(**valid_trade(side="SHORT", stop=61000, target=58000))


def test_paper_open_rejects_invalid_stop_direction():
    with pytest.raises(ValidationError, match="LONG stop must be below entry"):
        PaperOpen(**valid_trade(stop=61000))


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_paper_close_rejects_non_finite_exit_price(value):
    with pytest.raises(ValidationError):
        PaperClose(exit_price=value)


def make_client(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    return TestClient(main.app)


def stub_open_dependencies(monkeypatch, ticker):
    async def fake_ticker(exchange, symbol, market_type):
        if isinstance(ticker, Exception):
            raise ticker
        return {"last": ticker}

    async def fake_positions():
        return {"positions": []}

    async def fake_market_spec(exchange, symbol, market_type):
        return {"contract_size": 1.0, "contract_type": "spot", "amount_precision": None, "precision_mode": 2}

    monkeypatch.setattr(main.registry, "market_spec", fake_market_spec)
    monkeypatch.setattr(main.registry, "ticker", fake_ticker)
    monkeypatch.setattr(main, "get_positions", fake_positions)
    monkeypatch.setattr(main, "open_trade", lambda **kwargs: {"status": "OPEN", **kwargs})


def test_paper_open_endpoint_accepts_price_within_tolerance(monkeypatch):
    stub_open_dependencies(monkeypatch, 60000)
    response = make_client(monkeypatch).post("/api/paper/open", json=valid_trade(entry=62900, target=65000))
    assert response.status_code == 200
    assert response.json()["status"] == "OPEN"


def test_paper_open_endpoint_rejects_price_outside_tolerance(monkeypatch):
    stub_open_dependencies(monkeypatch, 60000)
    response = make_client(monkeypatch).post("/api/paper/open", json=valid_trade(entry=64000, target=65000))
    assert response.status_code == 400
    assert "maximum allowed deviation is 5.00%" in response.json()["detail"]


def test_paper_open_endpoint_rejects_missing_ticker_price(monkeypatch):
    stub_open_dependencies(monkeypatch, None)
    response = make_client(monkeypatch).post("/api/paper/open", json=valid_trade())
    assert response.status_code == 400
    assert "invalid market price" in response.json()["detail"]


def test_paper_open_endpoint_rejects_unknown_symbol_when_exchange_lookup_fails(monkeypatch):
    stub_open_dependencies(monkeypatch, ValueError("symbol not found"))
    response = make_client(monkeypatch).post("/api/paper/open", json=valid_trade(symbol="NOT/A-REAL-MARKET"))
    assert response.status_code == 400
    assert "symbol not found" in response.json()["detail"]


def test_paper_open_endpoint_rejects_non_finite_ticker(monkeypatch):
    stub_open_dependencies(monkeypatch, float("inf"))
    response = make_client(monkeypatch).post("/api/paper/open", json=valid_trade())
    assert response.status_code == 400
    assert "invalid market price" in response.json()["detail"]



def test_paper_open_endpoint_resolves_and_persists_canonical_futures_symbol(monkeypatch):
    seen = {}

    async def fake_market_spec(exchange, symbol, market_type):
        assert symbol == "BTC/USDT"
        assert market_type == "futures"
        return {
            "symbol": "BTC/USDT:USDT",
            "contract_size": 0.001,
            "contract_type": "linear",
            "amount_precision": 3,
            "precision_mode": 2,
        }

    async def fake_ticker(exchange, symbol, market_type):
        seen["ticker_symbol"] = symbol
        return {"last": 60000}

    async def fake_positions():
        return {"positions": []}

    def fake_open(**kwargs):
        seen["trade_symbol"] = kwargs["symbol"]
        return {"status": "OPEN", **kwargs}

    monkeypatch.setattr(main.registry, "market_spec", fake_market_spec)
    monkeypatch.setattr(main.registry, "ticker", fake_ticker)
    monkeypatch.setattr(main, "get_positions", fake_positions)
    monkeypatch.setattr(main, "open_trade", fake_open)

    payload = valid_trade(
        market_type="futures", side="LONG", entry=60000, stop=59000,
        target=62000, symbol="BTC/USDT",
    )
    response = make_client(monkeypatch).post("/api/paper/open", json=payload)
    assert response.status_code == 200
    assert seen["ticker_symbol"] == "BTC/USDT:USDT"
    assert seen["trade_symbol"] == "BTC/USDT:USDT"
