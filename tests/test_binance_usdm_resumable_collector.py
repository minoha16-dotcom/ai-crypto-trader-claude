from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import scripts.binance_usdm_resumable_collector as collector
from scripts.binance_usdm_resumable_collector import (
    BAR_HEADERS,
    HEADERS,
    _csv_values,
    _next_cursor,
    _row_timestamp,
    collect_stream,
    load_checkpoint,
    parse_ms,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if not self.pages:
            raise AssertionError("Unexpected extra API request")
        return FakeResponse(self.pages.pop(0))


def bar(timestamp: int, width: int = 12) -> list:
    values = [
        timestamp, "1", "2", "0.5", "1.5", "10",
        timestamp + 899999, "15", 3, "4", "5", "0",
    ]
    return values[:width]


def test_headers_match_binance_kline_csv_width():
    assert len(HEADERS["trade_15m"]) == 12
    assert len(HEADERS["mark_15m"]) == 12
    assert HEADERS["trade_15m"] == BAR_HEADERS
    assert HEADERS["mark_15m"] == BAR_HEADERS


def test_csv_values_rejects_short_or_long_kline_rows():
    with pytest.raises(ValueError, match="exactly 12"):
        _csv_values(bar(1000, width=6), "bar")
    with pytest.raises(ValueError, match="exactly 12"):
        _csv_values(bar(1000) + ["unexpected"], "bar")


def test_csv_values_requires_funding_fields():
    with pytest.raises(ValueError, match="fundingTime"):
        _csv_values({"fundingRate": "0.001"}, "funding")


def test_funding_cursor_uses_actual_timestamp_plus_one_ms():
    assert _next_cursor(1698883200001, "funding") == 1698883200002


def test_kline_cursor_advances_one_bar():
    assert _next_cursor(1_700_000_000_000, "bar") == 1_700_000_900_000


def test_funding_timestamp_requires_field():
    with pytest.raises(ValueError, match="fundingTime"):
        _row_timestamp({"fundingRate": "0.001"}, "funding")


def test_checkpoint_is_created_and_scope_checked(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    state = load_checkpoint(path, "BTCUSDT", 1000, 2000)
    assert state["symbol"] == "BTCUSDT"
    assert state["schema_version"] == collector.CHECKPOINT_SCHEMA_VERSION
    path.write_text(json.dumps(state), encoding="utf-8")
    assert load_checkpoint(path, "BTCUSDT", 1000, 2000)["streams"] == {}
    with pytest.raises(ValueError, match="scope mismatch"):
        load_checkpoint(path, "ETHUSDT", 1000, 2000)


def test_old_checkpoint_schema_is_rejected(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "symbol": "BTCUSDT",
            "start_ms": 1000,
            "end_ms_exclusive": 2000,
            "streams": {},
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unsupported checkpoint schema_version"):
        load_checkpoint(path, "BTCUSDT", 1000, 2000)


def test_crash_after_csv_fsync_before_checkpoint_recovers_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkpoint = tmp_path / "BTCUSDT-checkpoint.json"
    state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    session = FakeSession([bar(1000)])
    real_atomic_json = collector.atomic_json
    writes = 0

    def fail_page_checkpoint(path: Path, value: dict) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated crash before checkpoint replace")
        real_atomic_json(path, value)

    monkeypatch.setattr(collector, "atomic_json", fail_page_checkpoint)
    with pytest.raises(OSError, match="simulated crash"):
        collect_stream(
            session, tmp_path, "BTCUSDT", "trade_15m",
            1000, 901000, state, delay=0,
        )

    # Reload the durable checkpoint as a new process would.
    durable_state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    monkeypatch.setattr(collector, "atomic_json", real_atomic_json)
    resumed = FakeSession([bar(1000)])
    result = collect_stream(
        resumed, tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, durable_state, delay=0,
    )

    with (tmp_path / "BTCUSDT-trade_15m.csv").open(
        "r", newline="", encoding="utf-8"
    ) as handle:
        rows = list(csv.reader(handle))

    assert len(rows) == 2  # header plus one data row, never duplicated
    assert result["rows_written"] == 1
    assert result["complete"] is True
    assert resumed.calls == 1


def test_completed_stream_is_idempotent_on_repeat_run(tmp_path: Path):
    checkpoint = tmp_path / "BTCUSDT-checkpoint.json"
    state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    session = FakeSession([bar(1000)])
    collect_stream(
        session, tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, state, delay=0,
    )
    calls_after_first_run = session.calls
    result = collect_stream(
        session, tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, state, delay=0,
    )
    assert result["complete"] is True
    assert session.calls == calls_after_first_run == 1


def test_empty_page_remains_unresolved_and_incomplete(tmp_path: Path):
    checkpoint = tmp_path / "BTCUSDT-checkpoint.json"
    state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 10000)
    result = collect_stream(
        FakeSession([]), tmp_path, "BTCUSDT", "trade_15m",
        1000, 10000, state, delay=0,
    )
    assert result["complete"] is False
    assert result["empty_page_at_ms"] == 1000
    assert result["next_start_ms"] == 1000


def test_header_only_file_from_initialization_crash_is_recovered(tmp_path: Path):
    path = tmp_path / "BTCUSDT-trade_15m.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(HEADERS["trade_15m"])
    state = {"streams": {}}
    result = collect_stream(
        FakeSession([bar(1000)]), tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, state, delay=0,
    )
    assert result["rows_written"] == 1
    assert result["complete"] is True


def test_orphan_csv_with_data_fails_closed(tmp_path: Path):
    path = tmp_path / "BTCUSDT-trade_15m.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS["trade_15m"])
        writer.writerow(bar(1000))
    with pytest.raises(ValueError, match="without a matching checkpoint"):
        collect_stream(
            FakeSession([bar(1000)]), tmp_path, "BTCUSDT", "trade_15m",
            1000, 901000, {"streams": {}}, delay=0,
        )


def test_csv_tail_beyond_checkpoint_is_truncated_before_resume(tmp_path: Path):
    checkpoint = tmp_path / "BTCUSDT-checkpoint.json"
    state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    collect_stream(
        FakeSession([bar(1000)]), tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, state, delay=0,
    )
    path = tmp_path / "BTCUSDT-trade_15m.csv"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("uncommitted,partial,tail\n")
    durable_state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    collect_stream(
        FakeSession(), tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, durable_state, delay=0,
    )
    with path.open("r", newline="", encoding="utf-8") as handle:
        assert len(list(csv.reader(handle))) == 2


def test_checkpointed_csv_shorter_than_committed_offset_fails_closed(tmp_path: Path):
    checkpoint = tmp_path / "BTCUSDT-checkpoint.json"
    state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    collect_stream(
        FakeSession([bar(1000)]), tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, state, delay=0,
    )
    path = tmp_path / "BTCUSDT-trade_15m.csv"
    path.write_text("truncated", encoding="utf-8")
    durable_state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    with pytest.raises(ValueError, match="shorter than its checkpoint"):
        collect_stream(
            FakeSession(), tmp_path, "BTCUSDT", "trade_15m",
            1000, 901000, durable_state, delay=0,
        )


def test_parse_ms_requires_timezone():
    with pytest.raises(Exception):
        parse_ms("2023-11-01T00:00:00")


def test_parse_ms_normalizes_utc():
    assert parse_ms("2023-11-01T00:00:00Z") == 1698796800000


def test_same_size_csv_tampering_fails_checksum_validation(tmp_path: Path):
    checkpoint = tmp_path / "BTCUSDT-checkpoint.json"
    state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    collect_stream(
        FakeSession([bar(1000)]), tmp_path, "BTCUSDT", "trade_15m",
        1000, 901000, state, delay=0,
    )
    path = tmp_path / "BTCUSDT-trade_15m.csv"
    original = path.read_text(encoding="utf-8")
    tampered = original.replace(",1.5,", ",9.5,")
    assert len(tampered) == len(original)
    path.write_text(tampered, encoding="utf-8")
    durable_state = load_checkpoint(checkpoint, "BTCUSDT", 1000, 901000)
    with pytest.raises(ValueError, match="checksum disagrees"):
        collect_stream(
            FakeSession(), tmp_path, "BTCUSDT", "trade_15m",
            1000, 901000, durable_state, delay=0,
        )
