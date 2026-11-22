from pathlib import Path
import pytest
from scripts.vike_exploratory_train import BAR_MS, DAY_MS, HOUR_MS, month_bounds, resolve_csv, complete_day_bars, split_contiguous, context_returns


def test_month_bounds_are_utc_half_open():
    start, end = month_bounds("2024-02")
    assert end - start == 29 * DAY_MS
    assert start % DAY_MS == 0


def test_resolve_month_symbol_stream_layout(tmp_path: Path):
    folder = tmp_path / "raw-staging" / "2024-01" / "BTCUSDT"
    folder.mkdir(parents=True)
    file = folder / "trade_15m.csv"
    file.write_text("open_time\n", encoding="utf-8")
    assert resolve_csv(tmp_path / "raw-staging", "2024-01", "BTCUSDT", "trade_15m") == file.resolve()


def test_resolve_flat_month_symbol_layout(tmp_path: Path):
    folder = tmp_path / "raw-staging" / "2024-01"
    folder.mkdir(parents=True)
    file = folder / "BTCUSDT-trade_15m.csv"
    file.write_text("open_time\n", encoding="utf-8")
    assert resolve_csv(tmp_path / "raw-staging", "2024-01", "BTCUSDT", "trade_15m") == file.resolve()


def test_missing_stream_fails_closed(tmp_path: Path):
    raw = tmp_path / "raw-staging"
    raw.mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_csv(raw, "2024-01", "BTCUSDT", "trade_15m")


def test_complete_day_requires_both_trade_and_mark_96_bars():
    start = 1_704_067_200_000
    trade = {t: {"t": t} for t in range(start, start + DAY_MS, BAR_MS)}
    mark = dict(trade)
    eligible, excluded = complete_day_bars(trade, mark, start, start + DAY_MS)
    assert len(eligible) == 96 and excluded == []
    mark.pop(start + 5 * BAR_MS)
    eligible, excluded = complete_day_bars(trade, mark, start, start + DAY_MS)
    assert eligible == [] and len(excluded) == 1


def test_segments_never_bridge_missing_15m_bar():
    rows = [{"t": 0}, {"t": BAR_MS}, {"t": 3 * BAR_MS}]
    assert [len(s) for s in split_contiguous(rows)] == [2, 1]


def test_context_only_uses_closed_bars():
    closes = [i * HOUR_MS + HOUR_MS - 1 for i in range(6)]
    values = [100, 101, 102, 103, 104, 105]
    assert context_returns(closes, values, closes[4] - 1) == (0.0, 0.0)
    r1, r4 = context_returns(closes, values, closes[4])
    assert r1 == pytest.approx(104 / 103 - 1)
    assert r4 == pytest.approx(104 / 100 - 1)
