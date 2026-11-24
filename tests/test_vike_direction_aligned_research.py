from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.vike_direction_aligned_research import (
    BAR_MS, FEATURES, FOLDS, chronological_inner_split,
    direction_aligned_features, load_labeled_events, run_research, utc_ms,
    _group_diagnostics,
)


def raw_features(side="LONG"):
    return {
        "side": side, "ret_1": .01, "ret_4": .02, "ret_16": .03,
        "atr_pct": .01, "donchian_width_pct": .04,
        "channel_position": 1.1 if side == "LONG" else -.1,
        "volume_ratio": 2.0, "rsi14": .7, "hour_sin": .5, "hour_cos": .5,
        "htf_1h_ret_1": .01, "htf_1h_ret_4": .02,
        "htf_4h_ret_1": .03, "htf_4h_ret_4": .04,
    }


def event(ts, symbol="BTCUSDT", side="LONG", label=1, net_r=1.0):
    return {
        "symbol": symbol, "side": side, "timeframe": "15m",
        "signal_time_ms": ts, "entry_time_ms": ts + 1,
        "exit_time_ms": ts + 48 * BAR_MS, "label": label, "net_r": net_r,
        "features": direction_aligned_features(raw_features(side)),
    }


def test_short_alignment_mirrors_long_direction():
    long = direction_aligned_features(raw_features("LONG"))
    short = direction_aligned_features(raw_features("SHORT"))
    assert long["dir_ret_1"] == pytest.approx(.01)
    assert short["dir_ret_1"] == pytest.approx(-.01)
    assert long["dir_rsi14"] == pytest.approx(.7)
    assert short["dir_rsi14"] == pytest.approx(.3)
    assert long["dir_breakout_distance"] == pytest.approx(.1)
    assert short["dir_breakout_distance"] == pytest.approx(.1)
    assert len(long) == len(FEATURES) == 14


def test_feature_transform_rejects_bad_rsi():
    row = raw_features()
    row["rsi14"] = 70
    with pytest.raises(ValueError, match="normalized"):
        direction_aligned_features(row)


def test_inner_split_purges_training_labels_at_boundary():
    start = utc_ms("2024-01-01")
    rows = [event(start + i * BAR_MS, label=i % 2) for i in range(1000)]
    train, validation, boundary = chronological_inner_split(rows, utc_ms("2025-01-01"))
    assert boundary is not None and train and validation
    assert max(r["signal_time_ms"] for r in train) < boundary
    assert all(r["exit_time_ms"] < boundary - 48 * BAR_MS for r in train)
    assert min(r["signal_time_ms"] for r in validation) >= boundary
    assert max(r["signal_time_ms"] for r in train) < boundary
    assert all(r["signal_time_ms"] < utc_ms("2025-01-01") - 48 * BAR_MS
               and r["exit_time_ms"] < utc_ms("2025-01-01") - 48 * BAR_MS
               for r in train + validation)


def test_fold_periods_are_chronological_and_stop_before_holdout():
    ends = [utc_ms(fold["test_end"]) for fold in FOLDS]
    starts = [utc_ms(fold["test_start"]) for fold in FOLDS]
    assert all(start < end for start, end in zip(starts, ends))
    assert starts[1] == ends[0] and starts[2] == ends[1]
    assert ends[-1] == utc_ms("2026-05-01")


def test_wfo_fails_closed_when_group_sample_gate_not_met():
    rows = [event(utc_ms("2024-01-01") + i * BAR_MS, label=i % 2) for i in range(500)]
    report = run_research(rows, min_train_per_group=200, min_pooled_train=3000,
                          min_test_per_fold=50, bootstrap_reps=20)
    assert report["scored_fold_count"] == 0
    assert all(fold["status"] == "INCONCLUSIVE" for fold in report["folds"])
    assert report["acceptance"]["status"] == "NOT_APPROVED_PRICE_ONLY_SCOPE"


def test_synthetic_three_fold_wfo_reports_every_symbol_direction_group():
    rows = []
    base = utc_ms("2023-11-01")
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT",
               "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
    for si, symbol in enumerate(symbols):
        for side in ("LONG", "SHORT"):
            for i in range(240):
                ts = base + int(i * (18 * 30 * 24 * 60 * 60 * 1000 / 240))
                ts = ts - (ts % BAR_MS) + BAR_MS - 1
                label = i % 2
                row = event(ts, symbol, side, label, (0.2 if label else -0.3) + si * .001)
                row["features"]["dir_ret_1"] = (0.02 if label else -0.02) * (1 if side == "LONG" else -1)
                rows.append(row)
            for fold_start in ("2025-05-01", "2025-09-01", "2026-01-01"):
                start = utc_ms(fold_start)
                for i in range(60):
                    ts = start + i * 24 * 60 * 60 * 1000
                    ts = ts - (ts % BAR_MS) + BAR_MS - 1
                    label = i % 2
                    row = event(ts, symbol, side, label, (0.2 if label else -0.3) + si * .001)
                    row["features"]["dir_ret_1"] = (0.02 if label else -0.02) * (1 if side == "LONG" else -1)
                    rows.append(row)
    report = run_research(rows, min_train_per_group=200, min_pooled_train=3000,
                          min_test_per_fold=50, bootstrap_reps=100)
    assert report["scored_fold_count"] == 3
    assert report["oos_event_count"] > 100
    assert len(report["group_reports"]) == 3 * 16
    assert all(fold["status"] == "SCORED" for fold in report["folds"])


def test_loader_rejects_locked_holdout_events(tmp_path: Path):
    fields = [
        "symbol", "signal_time_ms", "entry_time_ms", "side", "label_status",
        "label", "net_r", "exit_time_ms", "ret_1", "ret_4", "ret_16",
        "atr_pct", "donchian_width_pct", "channel_position", "volume_ratio",
        "rsi14", "hour_sin", "hour_cos", "htf_1h_ret_1", "htf_1h_ret_4",
        "htf_4h_ret_1", "htf_4h_ret_4",
    ]
    row = {
        "symbol": "BTCUSDT", "signal_time_ms": utc_ms("2026-05-01") + BAR_MS - 1,
        "entry_time_ms": utc_ms("2026-05-01") + BAR_MS, "side": "LONG",
        "label_status": "LABELED_PRICE_ONLY", "label": 1, "net_r": 1.0,
        "exit_time_ms": utc_ms("2026-05-01") + 49 * BAR_MS - 1,
        "ret_1": .01, "ret_4": .02, "ret_16": .03, "atr_pct": .01,
        "donchian_width_pct": .04, "channel_position": 1.1, "volume_ratio": 2,
        "rsi14": .7, "hour_sin": .5, "hour_cos": .5,
        "htf_1h_ret_1": .01, "htf_1h_ret_4": .02,
        "htf_4h_ret_1": .03, "htf_4h_ret_4": .04,
    }
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(ValueError, match="outside the frozen development window"):
        load_labeled_events(path)


def test_loader_rejects_non_closed_signal_timestamp(tmp_path: Path):
    fields = [
        "symbol", "signal_time_ms", "entry_time_ms", "side", "label_status",
        "label", "net_r", "exit_time_ms", "ret_1", "ret_4", "ret_16",
        "atr_pct", "donchian_width_pct", "channel_position", "volume_ratio",
        "rsi14", "hour_sin", "hour_cos", "htf_1h_ret_1", "htf_1h_ret_4",
        "htf_4h_ret_1", "htf_4h_ret_4",
    ]
    start = utc_ms("2024-01-01")
    row = {
        "symbol": "BTCUSDT", "signal_time_ms": start + 1234,
        "entry_time_ms": start + 1235, "side": "LONG",
        "label_status": "LABELED_PRICE_ONLY", "label": 1, "net_r": 1.0,
        "exit_time_ms": start + 48 * BAR_MS,
        "ret_1": .01, "ret_4": .02, "ret_16": .03, "atr_pct": .01,
        "donchian_width_pct": .04, "channel_position": 1.1, "volume_ratio": 2,
        "rsi14": .7, "hour_sin": .5, "hour_cos": .5,
        "htf_1h_ret_1": .01, "htf_1h_ret_4": .02,
        "htf_4h_ret_1": .03, "htf_4h_ret_4": .04,
    }
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(ValueError, match="not a closed 15m candle close"):
        load_labeled_events(path)


def test_loader_rejects_label_that_disagrees_with_net_r(tmp_path: Path):
    fields = [
        "symbol", "signal_time_ms", "entry_time_ms", "side", "label_status",
        "label", "net_r", "exit_time_ms", "ret_1", "ret_4", "ret_16",
        "atr_pct", "donchian_width_pct", "channel_position", "volume_ratio",
        "rsi14", "hour_sin", "hour_cos", "htf_1h_ret_1", "htf_1h_ret_4",
        "htf_4h_ret_1", "htf_4h_ret_4",
    ]
    start = utc_ms("2024-01-01")
    signal = start - (start % BAR_MS) + BAR_MS - 1
    row = {
        "symbol": "BTCUSDT", "signal_time_ms": signal,
        "entry_time_ms": signal + 1, "side": "LONG",
        "label_status": "LABELED_PRICE_ONLY", "label": 1, "net_r": -0.5,
        "exit_time_ms": signal + 48 * BAR_MS,
        "ret_1": .01, "ret_4": .02, "ret_16": .03, "atr_pct": .01,
        "donchian_width_pct": .04, "channel_position": 1.1, "volume_ratio": 2,
        "rsi14": .7, "hour_sin": .5, "hour_cos": .5,
        "htf_1h_ret_1": .01, "htf_1h_ret_4": .02,
        "htf_4h_ret_1": .03, "htf_4h_ret_4": .04,
    }
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(ValueError, match="label does not match"):
        load_labeled_events(path)


def test_future_feature_availability_timestamp_is_rejected(tmp_path: Path):
    fields = [
        "symbol", "signal_time_ms", "entry_time_ms", "side", "label_status",
        "label", "net_r", "exit_time_ms", "feature_available_time_ms",
        "ret_1", "ret_4", "ret_16", "atr_pct", "donchian_width_pct",
        "channel_position", "volume_ratio", "rsi14", "hour_sin", "hour_cos",
        "htf_1h_ret_1", "htf_1h_ret_4", "htf_4h_ret_1", "htf_4h_ret_4",
    ]
    signal = utc_ms("2024-01-01") - (utc_ms("2024-01-01") % BAR_MS) + BAR_MS - 1
    row = {
        "symbol": "BTCUSDT", "signal_time_ms": signal,
        "entry_time_ms": signal + 1, "side": "LONG",
        "label_status": "LABELED_PRICE_ONLY", "label": 1, "net_r": 1.0,
        "exit_time_ms": signal + 48 * BAR_MS,
        "feature_available_time_ms": signal + 1,
        "ret_1": .01, "ret_4": .02, "ret_16": .03, "atr_pct": .01,
        "donchian_width_pct": .04, "channel_position": 1.1, "volume_ratio": 2,
        "rsi14": .7, "hour_sin": .5, "hour_cos": .5,
        "htf_1h_ret_1": .01, "htf_1h_ret_4": .02,
        "htf_4h_ret_1": .03, "htf_4h_ret_4": .04,
    }
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(ValueError, match="future feature leakage"):
        load_labeled_events(path)


def test_group_report_marks_missing_oos_group_inconclusive():
    btc = event(utc_ms("2025-05-02"), symbol="BTCUSDT")
    eth = event(utc_ms("2025-05-02"), symbol="ETHUSDT", side="SHORT")
    reports = _group_diagnostics(
        [btc], [0.7], [True], "fold_1",
        all_groups=[("BTCUSDT", "LONG", "15m"), ("ETHUSDT", "SHORT", "15m")],
        training_rows=[btc, eth],
    )
    missing = next(row for row in reports if row["symbol"] == "ETHUSDT")
    assert missing["status"] == "INCONCLUSIVE"
    assert missing["reason"] == "NO_MATURED_OOS_EVENTS_FOR_GROUP"
    assert missing["train_count"] == 1
    assert missing["test_count"] == 0
    assert missing["all_oos"] is None


def test_direction_aligned_features_do_not_depend_on_outcome_fields():
    raw = raw_features("SHORT")
    baseline = direction_aligned_features(raw)
    raw.update({"label": 1, "net_r": 9999.0, "exit_price": 1e12,
                "exit_time_ms": 9999999999999})
    assert direction_aligned_features(raw) == baseline


def test_feature_timestamps_accept_closed_1h_and_4h_context():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    signal = 14_399_999  # 04:00 UTC boundary, inclusive close timestamp
    row = {
        "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": signal,
        "htf_4h_close_time_ms": signal,
    }
    assert validate_feature_timestamps(row, signal)["htf_4h_close_time_ms"] == signal


def test_feature_timestamps_reject_missing_htf_provenance():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    row = {"feature_available_time_ms": 14_399_999, "htf_1h_close_time_ms": 14_399_999}
    with pytest.raises(ValueError, match="missing required htf_4h_close_time_ms"):
        validate_feature_timestamps(row, 14_399_999)


def test_feature_timestamps_reject_future_1h_close():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    signal = 14_399_999
    row = {
        "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": signal + 1,
        "htf_4h_close_time_ms": signal,
    }
    with pytest.raises(ValueError, match="future feature leakage"):
        validate_feature_timestamps(row, signal)


def test_feature_timestamps_reject_unclosed_1h_or_4h_boundary():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    signal = 14_399_999
    row = {
        "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": signal - 1,
        "htf_4h_close_time_ms": signal,
    }
    with pytest.raises(ValueError, match="not a closed UTC 1h boundary"):
        validate_feature_timestamps(row, signal)


def test_feature_timestamps_reject_stale_context():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    signal = 14_399_999
    row = {
        "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": signal - 3_600_000,
        "htf_4h_close_time_ms": signal,
    }
    with pytest.raises(ValueError, match="1h context timestamp is stale"):
        validate_feature_timestamps(row, signal)


def test_feature_timestamps_reject_future_4h_close():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    signal = 14_399_999
    row = {
        "feature_available_time_ms": signal,
        "htf_1h_close_time_ms": signal,
        "htf_4h_close_time_ms": signal + 14_400_000,
    }
    with pytest.raises(ValueError, match="future feature leakage"):
        validate_feature_timestamps(row, signal)


def test_feature_timestamps_reject_future_feature_computation_time():
    from scripts.vike_direction_aligned_research import validate_feature_timestamps
    signal = 14_399_999
    row = {
        "feature_available_time_ms": signal + 1,
        "htf_1h_close_time_ms": signal,
        "htf_4h_close_time_ms": signal,
    }
    with pytest.raises(ValueError, match="future feature leakage"):
        validate_feature_timestamps(row, signal)
