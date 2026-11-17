from datetime import datetime, timezone

import polars as pl
import pytest

from scripts.fetch_vike_hyperliquid_dataset import (
    START_POSITION_AVAILABLE_FROM_MS,
    _daily_dates,
    _fill_direction,
    _history_start_ms,
    _native_fill,
    _select_wallets_from_positions,
)


def _address(last_hex: str) -> str:
    return "0x" + ("0" * 39) + last_hex


def test_point_in_time_cohort_filters_exact_previous_utc_hour_and_label():
    hour_start = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
    rows = [
        {
            "wallet": _address("1"),
            "snapshot_ts": hour_start,
            "pnl_cohort": "3x smart",
        },
        {
            "wallet": _address("2"),
            "snapshot_ts": datetime(2026, 10, 15, 23, 59, tzinfo=timezone.utc),
            "pnl_cohort": "3x smart",
        },
        {
            # Prior hour is outside the selection hour.
            "wallet": _address("3"),
            "snapshot_ts": datetime(2026, 10, 15, 22, 59, tzinfo=timezone.utc),
            "pnl_cohort": "3x smart",
        },
        {
            # A different cohort in the correct hour is not selected.
            "wallet": _address("4"),
            "snapshot_ts": datetime(2026, 10, 15, 23, 15, tzinfo=timezone.utc),
            "pnl_cohort": "2x smart",
        },
        {
            # End timestamp is exclusive.
            "wallet": _address("5"),
            "snapshot_ts": datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc),
            "pnl_cohort": "3x smart",
        },
    ]
    frame = pl.DataFrame(rows).lazy()

    selected = _select_wallets_from_positions(
        frame,
        selection_start_ms=int(hour_start.timestamp() * 1000),
        selection_end_ms=int(datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
        cohort_label="3x smart",
        wallet_limit=10,
    )

    assert set(selected) == {_address("1"), _address("2")}
    assert len(selected) == 2


@pytest.mark.parametrize(
    ("start_position", "size", "side", "expected"),
    [
        (0.0, 1.0, "buy", "Open Long"),
        (0.0, 1.0, "sell", "Open Short"),
        (2.0, 1.0, "buy", "Add Long"),
        (2.0, 1.0, "sell", "Close Long"),
        (-2.0, 1.0, "sell", "Add Short"),
        (-2.0, 1.0, "buy", "Close Short"),
    ],
)
def test_fill_direction_uses_start_position_and_side(start_position, size, side, expected):
    assert _fill_direction(start_position, size, side) == expected


def test_fill_direction_rejects_unknown_or_nonpositive_size():
    assert _fill_direction(None, 1.0, "buy") == "UNCLASSIFIED"
    assert _fill_direction(0.0, 0.0, "buy") == "UNCLASSIFIED"
    assert _fill_direction(0.0, 1.0, "unknown") == "UNCLASSIFIED"


def test_native_fill_maps_vike_archive_schema_to_hyperliquid_position_schema():
    fill = _native_fill({
        "fill_ts": datetime(2026, 10, 16, 1, 2, tzinfo=timezone.utc),
        "wallet": _address("a"),
        "asset": "BTC",
        "side": "buy",
        "size_token": 0.25,
        "price": 65000.0,
        "fee": 1.2,
        "fee_token": "USDC",
        "order_id": 1234,
        "trade_id": 5678,
        "closed_pnl": -12.5,
        "start_position": -0.25,
    })

    assert fill["coin"] == "BTC"
    assert fill["time"] == int(datetime(2026, 10, 16, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
    assert fill["px"] == 65000.0
    assert fill["sz"] == 0.25
    assert fill["dir"] == "Close Short"
    assert fill["fee"] == 1.2
    assert fill["feeToken"] == "USDC"
    assert fill["oid"] == 1234
    assert fill["tid"] == 5678
    assert fill["closedPnl"] == -12.5
    assert fill["startPosition"] == -0.25


def test_daily_dates_respects_exclusive_end_boundary():
    start = int(datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    days = _daily_dates(start, end)

    assert len(days) == 31
    assert days[0] == "2026-10-16"
    assert days[-1] == "2026-11-15"


def test_default_warmup_starts_on_start_position_schema_availability_date():
    start = int(datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    assert _history_start_ms(start, 17) == START_POSITION_AVAILABLE_FROM_MS


def test_warmup_cannot_cross_before_start_position_schema_availability():
    start = int(datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    with pytest.raises(ValueError, match="start_position field"):
        _history_start_ms(start, 18)
