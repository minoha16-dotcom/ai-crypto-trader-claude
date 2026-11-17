from datetime import datetime, timezone

import pytest

from scripts import run_hyperliquid_strategy_research as runner


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body.encode("utf-8")


def test_parse_utc_requires_timezone_and_normalizes_to_utc():
    value = runner.parse_utc("2026-10-16T01:00:00+01:00")
    assert value == datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="explicit UTC offset"):
        runner.parse_utc("2026-10-16T00:00:00")


def test_vike_ndjson_parser_skips_metadata_and_sorts_candles(monkeypatch):
    payload = "\n".join([
        '{"symbol":"BTC","market":"perp","interval":"1h","source":"fills"}',
        '{"ts":3600000,"open":101,"high":102,"low":100,"close":101.5,"volume":10}',
        '{"ts":0,"open":100,"high":101,"low":99,"close":100.5,"volume":9}',
    ])
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: _Response(payload))
    candles = runner.fetch_ndjson_candles(
        "BTC", "1h", datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime(1970, 1, 1, 2, tzinfo=timezone.utc), "test-key",
    )
    assert [row[0] for row in candles] == [0, 3_600_000]
    assert candles[0][1:] == [100.0, 101.0, 99.0, 100.5, 9.0]


def test_vike_ndjson_parser_rejects_duplicate_timestamps(monkeypatch):
    row = '{"ts":3600000,"open":101,"high":102,"low":100,"close":101.5,"volume":10}'
    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: _Response(row + "\n" + row))
    with pytest.raises(ValueError, match="Duplicate"):
        runner.fetch_ndjson_candles(
            "BTC", "1h", datetime(1970, 1, 1, tzinfo=timezone.utc),
            datetime(1970, 1, 1, 2, tzinfo=timezone.utc), "test-key",
        )
