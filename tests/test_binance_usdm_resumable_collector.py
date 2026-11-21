from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.binance_usdm_resumable_collector import (
    _next_cursor, _row_timestamp, load_checkpoint, parse_ms,
)


def test_funding_cursor_uses_actual_timestamp_plus_one_ms():
    assert _next_cursor(1698883200001, "funding") == 1698883200002


def test_kline_cursor_advances_one_bar():
    assert _next_cursor(1_700_000_000_000, "bar") == 1_700_000_900_000


def test_funding_timestamp_requires_field():
    with pytest.raises(ValueError):
        _row_timestamp({"fundingRate": "0.001"}, "funding")


def test_checkpoint_is_created_and_scope_checked(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    state = load_checkpoint(path, "BTCUSDT", 1000, 2000)
    assert state["symbol"] == "BTCUSDT"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert load_checkpoint(path, "BTCUSDT", 1000, 2000)["streams"] == {}
    with pytest.raises(ValueError, match="scope mismatch"):
        load_checkpoint(path, "ETHUSDT", 1000, 2000)


def test_parse_ms_requires_timezone():
    with pytest.raises(Exception):
        parse_ms("2023-11-01T00:00:00")


def test_parse_ms_normalizes_utc():
    assert parse_ms("2023-11-01T00:00:00Z") == 1698796800000
