import hashlib

import pytest

from scripts.fetch_hyperliquid_leader_fills import (
    _fetch_time_range,
    extract_wallet_addresses,
)


def test_wallet_extractor_only_accepts_full_public_hex_addresses():
    payload = {
        "as_of": "2026-11-16T00:00:00Z",
        "leaderboardRows": [
            {"ethAddress": "0x" + "b" * 40, "displayName": "leader"},
            {"address": "0x" + "a" * 40},
            {"contract": "0x" + "c" * 40},
            {"address": "0x1234"},
        ],
    }

    payload["top_pnl"] = [
        {"ethAddress": "0x" + "b" * 40, "pnl30d": "500"},
        {"address": "0x" + "a" * 40, "pnl30d": "200"},
        {"address": "0x" + "c" * 40, "pnl30d": "-10"},
    ]
    assert extract_wallet_addresses(payload, cohort="top_pnl") == [
        "0x" + "b" * 40,
        "0x" + "a" * 40,
    ]


def test_range_fetch_splits_a_saturated_window(monkeypatch):
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


def test_range_fetch_fails_closed_when_page_cannot_be_split(monkeypatch):
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
        # Each subrange returns small unique records. The aggregate reaches the
        # hard 10,000-history safety threshold.
        start = body["startTime"]
        return [{"time": start + index, "tid": f"{start}-{index}"} for index in range(2000)]

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



def test_wallet_extractor_never_mixes_volume_cohort_into_top_pnl():
    payload = {
        "top_by_volume": [
            {"address": "0x" + "a" * 40, "volume30d": "999"},
        ],
        "top_by_pnl": [
            {"address": "0x" + "b" * 40, "pnl30d": "123"},
        ],
    }

    assert extract_wallet_addresses(payload, cohort="top_pnl") == ["0x" + "b" * 40]
    assert extract_wallet_addresses(payload, cohort="top_volume") == ["0x" + "a" * 40]


def test_wallet_extractor_rejects_unknown_json_layout_instead_of_scraping_all_addresses():
    payload = {
        "unrelated": [{"address": "0x" + "a" * 40}],
    }
    with pytest.raises(ValueError, match="refusing to select arbitrary wallet addresses"):
        extract_wallet_addresses(payload, cohort="top_pnl")


def test_user_funding_uses_the_smaller_time_range_page_limit(monkeypatch):
    from scripts.fetch_hyperliquid_leader_fills import MAX_PAGE_SIZE_BY_TYPE

    assert MAX_PAGE_SIZE_BY_TYPE["userFillsByTime"] == 2000
    assert MAX_PAGE_SIZE_BY_TYPE["userFunding"] == 500
