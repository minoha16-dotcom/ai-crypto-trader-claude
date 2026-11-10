import pytest
from pydantic import ValidationError

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
