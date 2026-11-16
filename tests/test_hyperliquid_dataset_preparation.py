import pytest

from scripts.prepare_hyperliquid_advisor_dataset import (
    aggregate_four_hour,
    features_before_entry,
    position_to_sample,
    reconstruct_closed_positions,
    _funding_for_position,
)


def _fill(direction, timestamp, price, size, oid, pnl=None, fee=0.1, start_position=None):
    return {
        "coin": "BTC",
        "dir": direction,
        "time": timestamp,
        "px": str(price),
        "sz": str(size),
        "startPosition": str(
            start_position if start_position is not None
            else (0.0 if direction.startswith("Open") else size)
        ),
        "oid": oid,
        "closedPnl": None if pnl is None else str(pnl),
        "fee": str(fee),
        "feeToken": "USDC",
        "tid": f"tid-{timestamp}-{oid}",
    }


def _wallet(fills, orders, funding=None):
    return {
        "address": "0x" + "a" * 40,
        "complete": True,
        "fills": fills,
        "funding": funding or [],
        "historical_orders": orders,
    }


def _trigger_order(
    oid,
    trigger_price,
    order_type="Take Profit Market",
    trigger_condition="Price above",
    side="A",
):
    return {
        "order": {
            "coin": "BTC",
            "oid": oid,
            "side": side,
            "isTrigger": True,
            "isPositionTpsl": True,
            "reduceOnly": True,
            "triggerPx": str(trigger_price),
            "triggerCondition": trigger_condition,
            "orderType": order_type,
        },
        "status": "filled",
        "statusTimestamp": 1_760_000_000_000,
    }


def _hourly_candles(count=130):
    candles = []
    for index in range(count):
        close = 100.0 + index * 0.1
        candles.append([
            index * 60 * 60 * 1000,
            close - 0.05,
            close + 0.4,
            close - 0.4,
            close,
            100.0 + index,
        ])
    return candles


def test_reconstructs_full_trigger_closed_long_and_accounts_for_fees_funding():
    fills = [
        _fill("Open Long", 10_000, 100.0, 2.0, 10),
        _fill("Close Long", 20_000, 110.0, 2.0, 20, pnl=20.0),
    ]
    funding = [{
        "time": 15_000,
        "delta": {"coin": "BTC", "usdc": "0.2"},
    }]
    wallet = _wallet(fills, [_trigger_order(20, 108.0)], funding)

    positions, exclusions = reconstruct_closed_positions(wallet)

    assert exclusions == {}
    assert len(positions) == 1
    position = positions[0]
    assert position["side"] == "LONG"
    assert position["entry_price"] == 100.0
    assert position["entry_quantity"] == 2.0
    assert position["closed_pnl_reported"] == pytest.approx(20.0)
    assert position["gross_pnl_reconstructed"] == pytest.approx(20.0)
    assert position["fees"] == pytest.approx(0.2)
    assert position["funding_paid"] == pytest.approx(0.2)
    assert position["exit_reason"] == "TARGET_GAP"


def test_reconstruct_manual_close_as_excluded_not_as_a_target_or_stop():
    fills = [
        _fill("Open Long", 10_000, 100.0, 1.0, 10),
        _fill("Close Long", 20_000, 99.0, 1.0, 20, pnl=-1.0),
    ]
    wallet = _wallet(fills, [])

    positions, _ = reconstruct_closed_positions(wallet)

    assert len(positions) == 1
    assert positions[0]["exit_reason"] == "MANUAL_CLOSE"


def test_reconstruct_different_close_orders_as_manual_not_single_trigger():
    fills = [
        _fill("Open Long", 10_000, 100.0, 2.0, 10),
        _fill("Close Long", 20_000, 110.0, 1.0, 20, pnl=10.0, start_position=2.0),
        _fill("Close Long", 30_000, 109.0, 1.0, 21, pnl=9.0, start_position=1.0),
    ]
    wallet = _wallet(
        fills,
        [_trigger_order(20, 108.0), _trigger_order(21, 108.0)],
    )

    positions, _ = reconstruct_closed_positions(wallet)

    assert len(positions) == 1
    assert positions[0]["exit_reason"] == "MANUAL_CLOSE"


def test_scaled_position_is_excluded_instead_of_inventing_single_entry_risk():
    fills = [
        _fill("Open Long", 10_000, 100.0, 1.0, 10),
        _fill("Add Long", 15_000, 105.0, 1.0, 11, start_position=1.0),
        _fill("Close Long", 20_000, 103.0, 2.0, 20, pnl=1.0, start_position=2.0),
    ]
    wallet = _wallet(fills, [_trigger_order(20, 102.0)])

    positions, exclusions = reconstruct_closed_positions(wallet)

    assert len(positions) == 1
    assert positions[0]["invalid_reason"] == "SCALED_POSITION"
    assert positions[0]["exit_reason"] == "UNKNOWN"
    assert exclusions["SCALED_POSITION"] >= 1


def test_incomplete_wallet_history_is_never_reconstructed():
    wallet = _wallet(
        [_fill("Open Long", 10_000, 100.0, 1.0, 10)],
        [],
    )
    wallet["complete"] = False

    positions, exclusions = reconstruct_closed_positions(wallet)

    assert positions == []
    assert exclusions["RAW_WALLET_INCOMPLETE"] == 1


def test_four_hour_aggregation_requires_four_consecutive_utc_hour_candles():
    hourly = _hourly_candles(16)
    four_hour = aggregate_four_hour(hourly)

    assert len(four_hour) == 4
    assert four_hour[0][0] == 0
    assert four_hour[0][1] == hourly[0][1]
    assert four_hour[0][2] == max(row[2] for row in hourly[:4])
    assert four_hour[0][3] == min(row[3] for row in hourly[:4])
    assert four_hour[0][4] == hourly[3][4]
    assert four_hour[0][5] == sum(row[5] for row in hourly[:4])

    gapped = [row for index, row in enumerate(hourly) if index != 5]
    assert len(aggregate_four_hour(gapped)) == 3


def test_features_use_only_fully_closed_candles_before_entry():
    candles = _hourly_candles(130)
    entry_timestamp = 120 * 60 * 60 * 1000

    features = features_before_entry(candles, entry_timestamp, "1h")

    assert set(features) == {
        "trend_composite", "rsi14", "adx14", "volume_ratio", "atr_pct"
    }
    assert all(value == value for value in features.values())


def test_position_sample_uses_system_normalized_r_and_keeps_manual_exit_unlabelled():
    base_position = {
        "coin": "BTC",
        "side": "LONG",
        "entry_price": 100.0,
        "entry_quantity": 2.0,
        "entry_timestamp": 120 * 60 * 60 * 1000,
        "exit_timestamp": 121 * 60 * 60 * 1000,
        "exit_reason": "TARGET",
        "closed_pnl_reported": 19.9,
        "gross_pnl_reconstructed": 20.0,
        "fees": 0.2,
        "funding_paid": 0.2,
        "atr14": 1.5,
    }
    candles = _hourly_candles(130)

    sample, problem = position_to_sample(
        base_position,
        wallet_address="0x" + "a" * 40,
        candles=candles,
        timeframe="1h",
    )

    assert problem is None
    assert sample["r_multiple"] == pytest.approx(20.0 / 6.0)
    assert sample["exit_reason"] == "TARGET"
    assert sample["position_side"] == "LONG"

    manual = dict(base_position, exit_reason="MANUAL_CLOSE")
    manual_sample, manual_problem = position_to_sample(
        manual,
        wallet_address="0x" + "a" * 40,
        candles=candles,
        timeframe="1h",
    )
    assert manual_problem is None
    assert "r_multiple" not in manual_sample
    assert manual_sample["exit_reason"] == "MANUAL_CLOSE"



def test_liquidation_is_retained_as_an_oos_signal_without_r_label():
    position = {
        "coin": "BTC",
        "side": "LONG",
        "entry_price": 100.0,
        "entry_quantity": 1.0,
        "entry_timestamp": 120 * 60 * 60 * 1000,
        "exit_timestamp": 121 * 60 * 60 * 1000,
        "exit_reason": "LIQUIDATION",
        "closed_pnl": -100.0,
        "fees": 0.2,
        "funding_paid": 0.0,
        "atr14": 1.5,
    }
    sample, reason = position_to_sample(
        position,
        wallet_address="0x" + "a" * 40,
        candles=_hourly_candles(130),
        timeframe="1h",
    )
    assert sample is not None
    assert reason == "LIQUIDATION"
    assert sample["exit_reason"] == "LIQUIDATION"
    assert sample["exclusion_reason"] == "LIQUIDATION"
    assert "r_multiple" not in sample


def test_reconstructable_but_invalid_scaled_position_is_retained_as_excluded_signal():
    position = {
        "coin": "BTC",
        "side": "LONG",
        "entry_price": 100.0,
        "entry_quantity": 2.0,
        "entry_timestamp": 120 * 60 * 60 * 1000,
        "exit_timestamp": 121 * 60 * 60 * 1000,
        "exit_reason": "UNKNOWN",
        "invalid_reason": "SCALED_POSITION",
        "closed_pnl": 10.0,
        "fees": 0.2,
        "funding_paid": 0.0,
        "atr14": 1.5,
    }
    sample, reason = position_to_sample(
        position,
        wallet_address="0x" + "a" * 40,
        candles=_hourly_candles(130),
        timeframe="1h",
    )
    assert sample is not None
    assert reason == "SCALED_POSITION"
    assert sample["exit_reason"] == "SCALED_POSITION"
    assert sample["exclusion_reason"] == "SCALED_POSITION"
    assert "r_multiple" not in sample



def test_start_position_mismatch_blocks_training_label():
    fills = [
        _fill("Open Long", 10_000, 100.0, 1.0, 10, start_position=0.0),
        _fill("Close Long", 20_000, 110.0, 1.0, 20, pnl=10.0, start_position=2.0),
    ]
    positions, exclusions = reconstruct_closed_positions(
        _wallet(fills, [_trigger_order(20, 108.0)])
    )

    assert len(positions) == 1
    assert positions[0]["invalid_reason"] == "START_POSITION_MISMATCH"
    assert positions[0]["exit_reason"] == "UNKNOWN"
    assert exclusions["START_POSITION_MISMATCH"] == 1


def test_r_label_does_not_double_subtract_fees_from_exchange_closed_pnl():
    position = {
        "coin": "BTC",
        "side": "LONG",
        "entry_price": 100.0,
        "entry_quantity": 2.0,
        "entry_timestamp": 120 * 60 * 60 * 1000,
        "exit_timestamp": 121 * 60 * 60 * 1000,
        "exit_reason": "TARGET",
        "closed_pnl_reported": 19.9,
        "gross_pnl_reconstructed": 20.0,
        "fees": 0.2,
        "funding_paid": 0.2,
        "atr14": 1.5,
    }
    sample, problem = position_to_sample(
        position,
        wallet_address="0x" + "a" * 40,
        candles=_hourly_candles(130),
        timeframe="1h",
    )

    assert problem is None
    # 20 gross - 0.2 signed fees + 0.2 funding = 20 net; 6 quote risk.
    assert sample["r_multiple"] == pytest.approx(20.0 / 6.0)



def test_empty_historical_order_list_is_valid_when_endpoint_reports_complete():
    from scripts.prepare_hyperliquid_advisor_dataset import raw_wallet_history_complete

    wallet = {
        "complete": True,
        "fills": [],
        "funding": [],
        "historical_orders": [],
        "fill_history": {"complete": True},
        "funding_history": {"complete": True},
        "historical_orders_status": {
            "rows": 0,
            "history_limit_reached": False,
            "complete_within_documented_limit": True,
        },
    }

    assert raw_wallet_history_complete(wallet) is True


def test_wallet_without_historical_order_completeness_flag_is_not_accepted():
    from scripts.prepare_hyperliquid_advisor_dataset import raw_wallet_history_complete

    wallet = {
        "complete": True,
        "fill_history": {"complete": True},
        "funding_history": {"complete": True},
        "historical_orders": [],
    }

    assert raw_wallet_history_complete(wallet) is False



def test_moved_stop_above_entry_is_still_classified_as_stop():
    fills = [
        _fill("Open Long", 10_000, 100.0, 1.0, 10),
        _fill("Close Long", 20_000, 104.0, 1.0, 20, pnl=4.0),
    ]
    stop = _trigger_order(
        20,
        105.0,
        order_type="Stop Market",
        trigger_condition="Price below 105",
        side="A",
    )
    positions, _ = reconstruct_closed_positions(_wallet(fills, [stop]))
    assert len(positions) == 1
    assert positions[0]["exit_reason"] == "STOP_GAP"


def test_short_take_profit_uses_buy_side_and_below_condition():
    fills = [
        _fill("Open Short", 10_000, 100.0, 1.0, 10, start_position=0.0),
        _fill("Close Short", 20_000, 90.0, 1.0, 20, pnl=10.0, start_position=-1.0),
    ]
    target = _trigger_order(
        20,
        92.0,
        order_type="Take Market",
        trigger_condition="Price below 92",
        side="B",
    )
    positions, _ = reconstruct_closed_positions(_wallet(fills, [target]))
    assert len(positions) == 1
    assert positions[0]["exit_reason"] == "TARGET_GAP"


def test_inconsistent_trigger_type_and_condition_is_unknown():
    fills = [
        _fill("Open Long", 10_000, 100.0, 1.0, 10),
        _fill("Close Long", 20_000, 110.0, 1.0, 20, pnl=10.0),
    ]
    invalid = _trigger_order(
        20,
        108.0,
        order_type="Stop Market",
        trigger_condition="Price above 108",
        side="A",
    )
    positions, _ = reconstruct_closed_positions(_wallet(fills, [invalid]))
    assert len(positions) == 1
    assert positions[0]["exit_reason"] == "UNKNOWN"


def test_partial_open_fills_use_last_open_fill_for_signal_and_first_for_funding():
    fills = [
        _fill("Open Long", 10_000, 100.0, 1.0, 10, start_position=0.0),
        _fill("Open Long", 12_000, 102.0, 1.0, 10, start_position=1.0),
        _fill("Close Long", 20_000, 110.0, 2.0, 20, pnl=18.0, start_position=2.0),
    ]
    funding = [{"time": 11_000, "delta": {"coin": "BTC", "usdc": "0.5"}}]
    positions, exclusions = reconstruct_closed_positions(
        _wallet(fills, [_trigger_order(20, 108.0)], funding)
    )
    assert exclusions == {}
    assert len(positions) == 1
    assert positions[0]["entry_timestamp"] == 12_000
    assert positions[0]["entry_start_timestamp"] == 10_000
    assert positions[0]["entry_price"] == pytest.approx(101.0)
    assert positions[0]["funding_paid"] == pytest.approx(0.5)


def test_partial_open_fill_after_reduction_invalidates_position():
    fills = [
        _fill("Open Long", 10_000, 100.0, 2.0, 10, start_position=0.0),
        _fill("Close Long", 15_000, 102.0, 1.0, 20, pnl=2.0, start_position=2.0),
        _fill("Open Long", 17_000, 103.0, 1.0, 10, start_position=1.0),
        _fill("Close Long", 25_000, 104.0, 2.0, 21, pnl=4.0, start_position=2.0),
    ]
    positions, exclusions = reconstruct_closed_positions(
        _wallet(fills, [_trigger_order(21, 103.0)])
    )
    assert len(positions) == 1
    assert positions[0]["invalid_reason"] == "ENTRY_FILL_AFTER_POSITION_REDUCTION"
    assert positions[0]["exit_reason"] == "UNKNOWN"
    assert exclusions["ENTRY_FILL_AFTER_POSITION_REDUCTION"] == 1


def test_malformed_funding_timestamp_invalidates_position_label():
    total, valid = _funding_for_position(
        [{"time": "invalid", "delta": {"coin": "BTC", "usdc": "0.5"}}],
        "BTC",
        10,
        20,
    )
    assert total == 0.0
    assert valid is False


def test_short_position_features_are_normalized_to_position_direction():
    candles = _hourly_candles(130)
    entry = 120 * 60 * 60 * 1000
    raw_features = features_before_entry(candles, entry, "1h")
    position = {
        "coin": "BTC",
        "side": "SHORT",
        "entry_price": 100.0,
        "entry_quantity": 1.0,
        "entry_timestamp": entry,
        "exit_timestamp": entry + 60 * 60 * 1000,
        "exit_reason": "TARGET",
        "gross_pnl_reconstructed": 5.0,
        "fees": 0.0,
        "funding_paid": 0.0,
        "atr14": 1.5,
    }
    sample, problem = position_to_sample(
        position,
        wallet_address="0x" + "a" * 40,
        candles=candles,
        timeframe="1h",
    )
    assert problem is None
    assert sample["position_side"] == "SHORT"
    assert sample["features"]["trend_composite"] == pytest.approx(-raw_features["trend_composite"])
    assert sample["features"]["rsi14"] == pytest.approx(100.0 - raw_features["rsi14"])
