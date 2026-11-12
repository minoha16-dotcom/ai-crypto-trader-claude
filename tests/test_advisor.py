import json
import pytest

from backend.advisor import (
    _features,
    analyze_closed_trade_history,
    load_training_examples,
    rank_opportunities,
)
from backend.config import settings
from backend.database import connect, init_db
from backend.paper import close_trade, open_trade


def test_advisor_learns_to_rank_setups_from_similar_closed_trades():
    winners = {
        "signal": "LONG", "score": 3, "rsi14": 68,
        "adx14": 42, "volume_ratio": 1.8, "atr_pct": 0.9,
    }
    losers = {
        "signal": "SHORT", "score": -3, "rsi14": 32,
        "adx14": 42, "volume_ratio": 1.8, "atr_pct": 0.9,
    }
    examples = [(_features(winners), 1.0)] * 9 + [(_features(winners), 0.0)]
    examples += [(_features(losers), 0.0)] * 9 + [(_features(losers), 1.0)]
    candidates = [dict(losers, symbol="LOSER/USDT"), dict(winners, symbol="WINNER/USDT")]

    result = rank_opportunities(candidates, examples, {
        "closed_paper_trades": 20,
        "winning_closed_trades": 10,
        "overall_historical_win_rate": 0.5,
        "signal_linked_closed_trades": 20,
        "examples_for_market_timeframe": 20,
    })

    assert result["mode"] == "local_closed_trade_similarity"
    assert result["best_candidate"]["symbol"] == "WINNER/USDT"
    assert result["best_candidate"]["estimated_historical_win_rate"] > 0.6
    assert result["best_candidate"]["similar_example_support"] > 1
    assert "لا تُفتح" in result["execution"]


def test_advisor_uses_explicit_cold_start_without_fake_win_probability():
    result = rank_opportunities([{
        "signal": "LONG", "score": 2, "adx14": 25,
        "volume_ratio": 1.2, "atr_pct": 0.5,
    }], [], {
        "closed_paper_trades": 0,
        "winning_closed_trades": 0,
        "overall_historical_win_rate": 0.5,
        "signal_linked_closed_trades": 0,
        "examples_for_market_timeframe": 0,
    })

    candidate = result["best_candidate"]
    assert result["mode"] == "technical_cold_start"
    assert candidate["ranking_method"] == "technical_cold_start"
    assert "estimated_historical_win_rate" not in candidate


def test_closed_trade_outcome_trains_from_linked_scan_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'advisor.db'}")
    init_db()
    setup = {
        "exchange": "kucoin", "symbol": "BTC/USDT", "market_type": "spot",
        "timeframe": "1h", "signal": "LONG", "score": 3,
        "price": 100.0, "rsi14": 65.0, "adx14": 35.0,
        "volume_ratio": 1.4, "atr_pct": 0.8, "trend": "bullish",
    }
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO signals(exchange,symbol,market_type,timeframe,signal,score,price,rsi,trend,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
            ("kucoin", "BTC/USDT", "spot", "1h", "LONG", 3, 100.0, 65.0, "bullish", json.dumps(setup)),
        )
        signal_id = cursor.fetchone()["id"]

    opened = open_trade(
        "kucoin", "BTC/USDT", "spot", "LONG", 100.0, 95.0, 110.0,
        signal_id=signal_id,
    )
    close_trade(opened["id"], 110.0)

    examples, counts = load_training_examples("spot", "1h")
    result = analyze_closed_trade_history([setup], "spot", "1h")
    assert len(examples) == 1
    assert examples[0][1] == 1.0
    assert counts["closed_paper_trades"] == 1
    assert counts["signal_linked_closed_trades"] == 1
    assert result["training"]["examples_for_market_timeframe"] == 1


def test_paper_trade_rejects_mismatched_linked_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'advisor-link.db'}")
    init_db()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO signals(exchange,symbol,market_type,timeframe,signal,score,price,rsi,trend,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
            ("kucoin", "BTC/USDT", "spot", "1h", "LONG", 2, 100.0, 60.0, "bullish", "{}"),
        )
        signal_id = cursor.fetchone()["id"]

    with pytest.raises(ValueError, match="does not match"):
        open_trade("kucoin", "ETH/USDT", "spot", "LONG", 100.0, 95.0, signal_id=signal_id)

