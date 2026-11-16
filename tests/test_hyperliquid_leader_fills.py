import json

import pytest

from scripts.fetch_hyperliquid_leader_fills import (
    MAX_PAGE_SIZE_BY_TYPE,
    _fetch_time_range,
    _load_leaderboard,
    extract_wallet_addresses,
)


def _row(address, pnl, volume):
    return {
        "ethAddress": address,
        "windowPerformances": [
            ["day", {"pnl": "1", "roi": "0.01", "vlm": "10"}],
            ["week", {"pnl": "2", "roi": "0.02", "vlm": "20"}],
            ["month", {"pnl": str(pnl), "roi": "0.05", "vlm": str(volume)}],
            ["allTime", {"pnl": "5", "roi": "0.1", "vlm": "100"}],
        ],
    }


def _leaderboard_rows():
    rows = []
    for index in range(100):
        rows.append(_row(f"0x{index + 1:040x}", index - 20, 1000 + index))
    rows.extend([
        _row("0x" + "a" * 40, 5000, 500),
        _row("0x" + "b" * 40, 1000, 9000),
    ])
    return {"leaderboardRows": rows}


def test_wallet_extractor_ranks_real_hyperliquid_schema_by_positive_month_pnl():
    payload = _leaderboard_rows()
    addresses = extract_wallet_addresses(payload, cohort="top_pnl")

    assert addresses[0] == "0x" + "a" * 40
    assert addresses[1] == "0x" + "b" * 40
    assert len(addresses) == 81
    assert all(address.startswith("0x") and len(address) == 42 for address in addresses)


def test_wallet_extractor_uses_distinct_volume_cohort_when_explicitly_requested():
    payload = _leaderboard_rows()
    pnl_ranked = extract_wallet_addresses(payload, cohort="top_pnl")
    volume_ranked = extract_wallet_addresses(payload, cohort="top_volume")

    assert pnl_ranked[0] == "0x" + "a" * 40
    assert volume_ranked[0] == "0x" + "b" * 40
    assert set(pnl_ranked) != set(volume_ranked)


def test_wallet_extractor_rejects_unknown_schema_instead_of_scraping_arbitrary_addresses():
    payload = {"wallets": [{"address": "0x" + "a" * 40}]}
    with pytest.raises(ValueError, match="leaderboard schema"):
        extract_wallet_addresses(payload, cohort="top_pnl")


class _FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.raw


def test_leaderboard_loader_reads_raw_board_and_separate_snapshot_metadata(monkeypatch):
    from scripts import fetch_hyperliquid_leader_fills as module

    metadata = {
        "as_of": "2026-11-16T10:18:04.823Z",
        "license": "CC BY 4.0",
    }
    board = _leaderboard_rows()
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url == "https://metadata.invalid/snapshot.json":
            return _FakeResponse(metadata)
        if request.full_url == "https://board.invalid/leaderboard":
            return _FakeResponse(board)
        raise AssertionError(f"unexpected URL {request.full_url}")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    raw_board, raw_metadata, returned_metadata, addresses, snapshot = _load_leaderboard(
        "https://board.invalid/leaderboard",
        "https://metadata.invalid/snapshot.json",
        1,
        cohort="top_pnl",
    )

    assert len(raw_board) > 0
    assert len(raw_metadata) > 0
    assert returned_metadata == metadata
    assert addresses[0] == "0x" + "a" * 40
    assert snapshot == "2026-11-16T10:18:04.823Z"
    assert calls == [
        "https://metadata.invalid/snapshot.json",
        "https://board.invalid/leaderboard",
    ]


def test_range_fetch_splits_saturated_windows(monkeypatch):
    calls = []

    def fake_post(body, timeout):
        calls.append(body)
        start = body["startTime"]
        end = body["endTime"]
        if end - start > 1000:
            return [{"time": index} for index in range(2000)]
        return [{"time": start}, {"time": end}]

    monkeypatch.setattr(
        "scripts.fetch_hyperliquid_leader_fills._post_info",
        fake_post,
    )
    rows, status = _fetch_time_range(
        "0x" + "a" * 40,
        "userFillsByTime",
        0,
        10_000,
        timeout=1,
        pause_seconds=0,
    )

    assert len(calls) > 1
    assert status["complete"] is True
    assert rows == sorted(rows, key=lambda row: row["time"])


def test_range_fetch_fails_closed_when_saturated_leaf_cannot_be_split(monkeypatch):
    def fake_post(body, timeout):
        return [{"time": 1}] * 2000

    monkeypatch.setattr(
        "scripts.fetch_hyperliquid_leader_fills._post_info",
        fake_post,
    )
    rows, status = _fetch_time_range(
        "0x" + "a" * 40,
        "userFillsByTime",
        0,
        500,
        timeout=1,
        pause_seconds=0,
    )

    assert status["complete"] is False
    assert status["response_page_truncated"] is True


def test_range_fetch_marks_the_exchange_history_cap(monkeypatch):
    def fake_post(body, timeout):
        start = body["startTime"]
        return [
            {"time": start + index, "tid": f"{start}-{index}"}
            for index in range(2000)
        ]

    monkeypatch.setattr(
        "scripts.fetch_hyperliquid_leader_fills._post_info",
        fake_post,
    )
    rows, status = _fetch_time_range(
        "0x" + "a" * 40,
        "userFillsByTime",
        0,
        100_000,
        timeout=1,
        pause_seconds=0,
        max_depth=3,
    )

    assert status["history_limit_reached"] is True
    assert status["complete"] is False


def test_endpoint_page_caps_are_explicit_and_distinct():
    assert MAX_PAGE_SIZE_BY_TYPE["userFillsByTime"] == 2000
    assert MAX_PAGE_SIZE_BY_TYPE["userFunding"] == 500
