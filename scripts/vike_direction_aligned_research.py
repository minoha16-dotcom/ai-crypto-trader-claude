#!/usr/bin/env python3
"""Reproducible Direction-Aligned Logistic Regression diagnostics on funding-aware labels.

Consumes only the separate funding-aware development label bundle. It never reads
raw-staging, fetches exchange data, mutates source files, accesses the locked
May-Oct 2026 holdout, or enables trading. Funding cashflows are applied, but
maintenance margin, leverage, and liquidation are not modeled.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

BAR_MS = 15 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
FOUR_HOUR_MS = 4 * HOUR_MS
EMBARGO_MS = 48 * BAR_MS
FEATURES = (
    "dir_ret_1", "dir_ret_4", "dir_ret_16",
    "log_atr_pct", "log_donchian_width_pct", "dir_breakout_distance",
    "log_volume_ratio", "dir_rsi14", "hour_sin", "hour_cos",
    "dir_htf_1h_ret_1", "dir_htf_1h_ret_4",
    "dir_htf_4h_ret_1", "dir_htf_4h_ret_4",
)
C_GRID_DEFAULT = (0.001, 0.01, 0.1, 1.0, 10.0)
FROZEN_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT")
FOLDS = (
    {"name": "fold_1", "test_start": "2025-05-01", "test_end": "2025-09-01"},
    {"name": "fold_2", "test_start": "2025-09-01", "test_end": "2026-01-01"},
    {"name": "fold_3", "test_start": "2026-01-01", "test_end": "2026-05-01"},
)
DEVELOPMENT_START = "2023-11-01"
DEVELOPMENT_END = "2026-05-01"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_ms(date_text: str) -> int:
    dt = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def validate_feature_timestamps(row: dict, signal_time_ms: int, line: int = 0) -> dict:
    """Require feature and HTF source close times to be causal and correctly aligned."""
    parsed = {}
    for key in ("feature_available_time_ms", "htf_1h_close_time_ms", "htf_4h_close_time_ms"):
        value = row.get(key)
        if value in (None, ""):
            raise ValueError(f"line {line}: missing required {key}; causal availability cannot be proven")
        try:
            parsed[key] = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"line {line}: invalid {key}") from exc
        if parsed[key] > signal_time_ms:
            raise ValueError(f"line {line}: future feature leakage: {key} is after signal_time_ms")
    if parsed["feature_available_time_ms"] != signal_time_ms:
        raise ValueError(f"line {line}: feature_available_time_ms must equal the closed 15m signal timestamp")
    if parsed["htf_1h_close_time_ms"] % HOUR_MS != HOUR_MS - 1:
        raise ValueError(f"line {line}: htf_1h_close_time_ms is not a closed UTC 1h boundary")
    if parsed["htf_4h_close_time_ms"] % FOUR_HOUR_MS != FOUR_HOUR_MS - 1:
        raise ValueError(f"line {line}: htf_4h_close_time_ms is not a closed UTC 4h boundary")
    if signal_time_ms - parsed["htf_1h_close_time_ms"] >= HOUR_MS:
        raise ValueError(f"line {line}: 1h context timestamp is stale or incomplete")
    if signal_time_ms - parsed["htf_4h_close_time_ms"] >= FOUR_HOUR_MS:
        raise ValueError(f"line {line}: 4h context timestamp is stale or incomplete")
    return parsed


def _number(row: dict, key: str) -> float:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"missing/non-numeric {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite {key}")
    return value


def direction_aligned_features(row: dict) -> dict[str, float]:
    """Map LONG/SHORT into one direction-neutral feature coordinate system."""
    side = str(row.get("side", "")).upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    sign = 1.0 if side == "LONG" else -1.0
    raw = {key: _number(row, key) for key in (
        "ret_1", "ret_4", "ret_16", "atr_pct", "donchian_width_pct",
        "channel_position", "volume_ratio", "rsi14", "hour_sin", "hour_cos",
        "htf_1h_ret_1", "htf_1h_ret_4", "htf_4h_ret_1", "htf_4h_ret_4",
    )}
    if raw["atr_pct"] <= 0 or raw["donchian_width_pct"] <= 0 or raw["volume_ratio"] < 0:
        raise ValueError("ATR/channel width must be positive and volume ratio nonnegative")
    if not 0.0 <= raw["rsi14"] <= 1.0:
        raise ValueError("rsi14 must be normalized to [0, 1]")
    # Donchian position is >1 for LONG breakouts and <0 for SHORT breakouts.
    # Distance is measured outward from the relevant channel edge.
    breakout_distance = raw["channel_position"] - 1.0 if sign > 0 else -raw["channel_position"]
    result = {
        "dir_ret_1": sign * raw["ret_1"],
        "dir_ret_4": sign * raw["ret_4"],
        "dir_ret_16": sign * raw["ret_16"],
        "log_atr_pct": math.log(raw["atr_pct"]),
        "log_donchian_width_pct": math.log(raw["donchian_width_pct"]),
        "dir_breakout_distance": breakout_distance,
        "log_volume_ratio": math.log1p(raw["volume_ratio"]),
        "dir_rsi14": raw["rsi14"] if sign > 0 else 1.0 - raw["rsi14"],
        "hour_sin": raw["hour_sin"],
        "hour_cos": raw["hour_cos"],
        "dir_htf_1h_ret_1": sign * raw["htf_1h_ret_1"],
        "dir_htf_1h_ret_4": sign * raw["htf_1h_ret_4"],
        "dir_htf_4h_ret_1": sign * raw["htf_4h_ret_1"],
        "dir_htf_4h_ret_4": sign * raw["htf_4h_ret_4"],
    }
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("direction-aligned features contain non-finite values")
    return result


def load_labeled_events(labels_csv: Path) -> list[dict]:
    required = {
        "symbol", "signal_time_ms", "entry_time_ms", "side", "label_status",
        "label", "net_r", "exit_time_ms", "ret_1", "ret_4", "ret_16",
        "atr_pct", "donchian_width_pct", "channel_position", "volume_ratio",
        "rsi14", "hour_sin", "hour_cos", "htf_1h_ret_1", "htf_1h_ret_4",
        "htf_4h_ret_1", "htf_4h_ret_4",
    }
    rows, seen, observed_scopes = [], set(), set()
    with labels_csv.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"labels.csv missing columns: {sorted(missing)}")
        for line, raw in enumerate(reader, 2):
            label_status = raw.get("label_status")
            if label_status not in ("LABELED_FUNDING_AWARE", "LABELED_PRICE_ONLY"):
                continue
            observed_scopes.add(label_status)
            if label_status == "LABELED_FUNDING_AWARE":
                for required_key in ("feature_available_time_ms", "htf_1h_close_time_ms",
                                     "htf_4h_close_time_ms", "funding_cashflow_per_unit",
                                     "funding_events_applied"):
                    if required_key not in (reader.fieldnames or []):
                        raise ValueError(f"line {line}: funding-aware labels missing {required_key}")
                _number(raw, "funding_cashflow_per_unit")
                if int(raw["funding_events_applied"]) < 0:
                    raise ValueError(f"line {line}: funding_events_applied must be nonnegative")
            symbol = str(raw["symbol"]).upper()
            side = str(raw["side"]).upper()
            signal, entry, exit_time = (int(raw[k]) for k in ("signal_time_ms", "entry_time_ms", "exit_time_ms"))
            if side not in ("LONG", "SHORT") or symbol not in FROZEN_SYMBOLS:
                raise ValueError(f"line {line}: invalid symbol/side or symbol outside frozen universe")
            if entry != signal + 1 or exit_time <= entry:
                raise ValueError(f"line {line}: invalid signal/entry/exit ordering")
            if signal % BAR_MS != BAR_MS - 1:
                raise ValueError(f"line {line}: signal timestamp is not a closed 15m candle close")
            if signal < utc_ms(DEVELOPMENT_START) or signal >= utc_ms(DEVELOPMENT_END):
                raise ValueError(f"line {line}: event outside the frozen development window")
            if exit_time - signal > 48 * BAR_MS:
                raise ValueError(f"line {line}: label exceeds the frozen 48-bar maturity horizon")
            if label_status == "LABELED_FUNDING_AWARE" or any(
                raw.get(key) not in (None, "") for key in (
                    "feature_available_time_ms", "htf_1h_close_time_ms", "htf_4h_close_time_ms"
                )
            ):
                validate_feature_timestamps(raw, signal, line)
            key = (symbol, side, signal)
            if key in seen:
                raise ValueError(f"line {line}: duplicate symbol/side/signal event {key}")
            seen.add(key)
            label_value = int(raw["label"])
            net_r = _number(raw, "net_r")
            if label_value not in (0, 1):
                raise ValueError(f"line {line}: label must be binary")
            if label_value != int(net_r > 0.0):
                raise ValueError(f"line {line}: label does not match the frozen net_r > 0 definition")
            event = {
                "symbol": symbol, "side": side, "timeframe": "15m",
                "signal_time_ms": signal, "entry_time_ms": entry, "exit_time_ms": exit_time,
                "label": label_value, "net_r": net_r,
            }
            event["features"] = direction_aligned_features(raw)
            rows.append(event)
    if len(observed_scopes) > 1:
        raise ValueError("mixed Price-only and Funding-aware labels are not allowed in one research run")
    if not rows:
        raise ValueError("no labeled research rows found")
    return sorted(rows, key=lambda item: (item["signal_time_ms"], item["symbol"], item["side"]))


def chronological_inner_split(train_rows: list[dict], test_start_ms: int, embargo_ms: int = EMBARGO_MS):
    """Chronological 80/20 inner split; labels must mature before the boundary."""
    eligible = [r for r in train_rows if r["signal_time_ms"] < test_start_ms - embargo_ms
                and r["exit_time_ms"] < test_start_ms - embargo_ms]
    times = sorted({r["signal_time_ms"] for r in eligible})
    if len(times) < 5:
        return [], [], None
    boundary_index = max(1, min(len(times) - 1, int(len(times) * 0.8)))
    validation_start = times[boundary_index]
    inner_train = [r for r in eligible if r["signal_time_ms"] < validation_start
                   and r["exit_time_ms"] < validation_start - embargo_ms]
    validation = [r for r in eligible if r["signal_time_ms"] >= validation_start]
    return inner_train, validation, validation_start


def _auc(y_true, scores):
    if len(set(y_true)) < 2:
        return None
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y_true, scores))


def _fit_pipeline(rows: list[dict], c_value: float):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    x = np.asarray([[r["features"][name] for name in FEATURES] for r in rows], dtype=float)
    y = np.asarray([r["label"] for r in rows], dtype=int)
    if len(set(y)) < 2:
        raise ValueError("training partition has only one label class")
    model = Pipeline([
        ("scale", StandardScaler()),
        ("logistic", LogisticRegression(C=c_value, max_iter=3000, solver="lbfgs", random_state=31)),
    ])
    model.fit(x, y)
    return model


def _predict(model, rows: list[dict]) -> list[float]:
    import numpy as np
    x = np.asarray([[r["features"][name] for name in FEATURES] for r in rows], dtype=float)
    return [float(value) for value in model.predict_proba(x)[:, 1]]


def select_c(inner_train: list[dict], validation: list[dict], c_grid: Iterable[float]):
    """Choose C by validation ROC-AUC only; no OOS/test outcomes are inspected."""
    y_val = [r["label"] for r in validation]
    y_inner = {r["label"] for r in inner_train}
    if len(inner_train) < 30 or len(y_inner) < 2 or len(validation) < 30 or len(set(y_val)) < 2:
        return None, {"status": "INCONCLUSIVE_VALIDATION", "c_scores": []}
    scores = []
    for c_value in c_grid:
        model = _fit_pipeline(inner_train, float(c_value))
        probabilities = _predict(model, validation)
        auc = _auc(y_val, probabilities)
        scores.append({"C": float(c_value), "validation_auc": auc})
    valid = [row for row in scores if row["validation_auc"] is not None]
    if not valid:
        return None, {"status": "INCONCLUSIVE_VALIDATION", "c_scores": scores}
    best = max(valid, key=lambda row: (row["validation_auc"], -row["C"]))
    return best["C"], {"status": "SELECTED_ON_INNER_VALIDATION", "selected_C": best["C"], "c_scores": scores}


def _mean(values):
    return float(statistics.fmean(values)) if values else None


def _classification_metrics(rows: list[dict], probabilities: list[float]) -> dict:
    y = [r["label"] for r in rows]
    return {
        "n": len(rows), "positive_rate": _mean(y), "roc_auc": _auc(y, probabilities),
        "mean_net_r": _mean([r["net_r"] for r in rows]),
        "median_net_r": float(statistics.median(r["net_r"] for r in rows)) if rows else None,
    }


def _bootstrap_uplift(rows: list[dict], selected: list[bool], reps: int = 2000, seed: int = 20261124):
    """UTC-week cluster bootstrap; all symbols in a week move together."""
    import random
    groups = defaultdict(list)
    for row, is_selected in zip(rows, selected):
        groups[row["signal_time_ms"] // (7 * 24 * 60 * 60 * 1000)].append((row["net_r"], is_selected))
    keys = sorted(groups)
    has_selected = any(flag for values in groups.values() for _, flag in values)
    if len(keys) < 8 or not has_selected:
        return {"method": "UTC_WEEK_CLUSTER_PERCENTILE_BOOTSTRAP", "repetitions": reps,
                "ci_95": None, "reason": "insufficient_week_clusters_or_selected_events"}
    rng = random.Random(seed)
    uplifts = []
    for _ in range(reps):
        sampled = [rng.choice(keys) for _ in keys]
        all_values, selected_values = [], []
        for key in sampled:
            for value, flag in groups[key]:
                all_values.append(value)
                if flag:
                    selected_values.append(value)
        if selected_values and all_values:
            uplifts.append(statistics.fmean(selected_values) - statistics.fmean(all_values))
    if len(uplifts) < reps * 0.8:
        ci, reason = None, "too_many_bootstrap_samples_without_selected_events"
    else:
        uplifts.sort()
        ci = [uplifts[int(0.025 * (len(uplifts) - 1))], uplifts[int(0.975 * (len(uplifts) - 1))]]
        reason = None
    return {"method": "UTC_WEEK_CLUSTER_PERCENTILE_BOOTSTRAP", "repetitions": reps,
            "week_clusters": len(keys), "ci_95": ci, "reason": reason}


def _group_key(row):
    return (row["symbol"], row["side"], row["timeframe"])


def _group_diagnostics(rows: list[dict], probabilities: list[float], selected: list[bool],
                       fold_name: str, all_groups: Iterable[tuple[str, str, str]] = (),
                       training_rows: Iterable[dict] = ()):
    grouped = defaultdict(list)
    train_counts = defaultdict(int)
    for row, prob, take in zip(rows, probabilities, selected):
        grouped[_group_key(row)].append((row, prob, take))
    for row in training_rows:
        train_counts[_group_key(row)] += 1
    for group in all_groups:
        grouped.setdefault(group, [])
    result = []
    for (symbol, side, timeframe), items in sorted(grouped.items()):
        group_rows = [item[0] for item in items]
        group_probs = [item[1] for item in items]
        chosen = [item[0]["net_r"] for item in items if item[2]]
        all_r = [r["net_r"] for r in group_rows]
        base = {
            "fold": fold_name, "symbol": symbol, "direction": side,
            "timeframe": timeframe, "train_count": train_counts[(symbol, side, timeframe)],
            "test_count": len(group_rows), "selected_count": len(chosen),
        }
        if not group_rows:
            result.append({
                **base, "status": "INCONCLUSIVE",
                "reason": "NO_MATURED_OOS_EVENTS_FOR_GROUP",
                "all_oos": None, "selected_mean_net_r": None,
                "all_mean_net_r": None, "selected_minus_all_net_r": None,
            })
            continue
        result.append({
            **base, "status": "SCORED",
            "all_oos": _classification_metrics(group_rows, group_probs),
            "selected_mean_net_r": _mean(chosen),
            "all_mean_net_r": _mean(all_r),
            "selected_minus_all_net_r": (_mean(chosen) - _mean(all_r)) if chosen else None,
            "reason": None if chosen else "NO_EVENTS_ABOVE_VALIDATION_THRESHOLD",
        })
    return result


def run_research(rows: list[dict], c_grid: Iterable[float] = C_GRID_DEFAULT,
                 min_train_per_group: int = 200, min_pooled_train: int = 3000,
                 min_test_per_fold: int = 50, bootstrap_reps: int = 2000,
                 expected_symbols: Iterable[str] | None = None) -> dict:
    import sklearn
    import numpy as np
    results, oos_predictions, group_reports = [], [], []
    for fold in FOLDS:
        test_start, test_end = utc_ms(fold["test_start"]), utc_ms(fold["test_end"])
        training = [r for r in rows if r["signal_time_ms"] < test_start - EMBARGO_MS
                    and r["exit_time_ms"] < test_start - EMBARGO_MS]
        test = [r for r in rows if test_start <= r["signal_time_ms"] < test_end - EMBARGO_MS
                and r["exit_time_ms"] < test_end]
        train_group_counts = defaultdict(int)
        for row in training:
            train_group_counts[_group_key(row)] += 1
        symbols = tuple(expected_symbols) if expected_symbols is not None else tuple(sorted({r["symbol"] for r in rows}))
        all_groups = sorted(
            {_group_key(r) for r in rows}
            | {(symbol, side, "15m") for symbol in symbols for side in ("LONG", "SHORT")}
        )
        missing_groups = ["|".join(group) for group in all_groups
                          if train_group_counts[group] < min_train_per_group]
        fold_report = {"fold": fold["name"], "test_start": fold["test_start"],
                       "test_end_exclusive": fold["test_end"], "status": "INCONCLUSIVE",
                       "train_count": len(training), "test_count": len(test),
                       "train_group_counts": {"|".join(k): v for k, v in sorted(train_group_counts.items())},
                       "groups_below_min_train": missing_groups}
        if len(training) < min_pooled_train or len(test) < min_test_per_fold or missing_groups:
            fold_report["reason"] = "MINIMUM_TRAINING_OR_TEST_SAMPLE_GATE_FAILED"
            for symbol, side, timeframe in all_groups:
                group_reports.append({
                    "fold": fold["name"], "symbol": symbol, "direction": side,
                    "timeframe": timeframe, "status": "INCONCLUSIVE",
                    "train_count": train_group_counts[(symbol, side, timeframe)],
                    "test_count": sum(_group_key(r) == (symbol, side, timeframe) for r in test),
                    "reason": fold_report["reason"],
                })
            results.append(fold_report)
            continue
        inner_train, validation, validation_start = chronological_inner_split(training, test_start)
        if len(inner_train) < min_pooled_train * 0.5:
            fold_report["reason"] = "INNER_TRAINING_SAMPLE_TOO_SMALL"
            for symbol, side, timeframe in all_groups:
                group_reports.append({"fold": fold["name"], "symbol": symbol,
                    "direction": side, "timeframe": timeframe, "status": "INCONCLUSIVE",
                    "train_count": sum(_group_key(r) == (symbol, side, timeframe) for r in training),
                    "test_count": sum(_group_key(r) == (symbol, side, timeframe) for r in test),
                    "reason": fold_report["reason"]})
            results.append(fold_report)
            continue
        selected_c, selection = select_c(inner_train, validation, c_grid)
        fold_report["inner_validation"] = {
            "start_utc": datetime.fromtimestamp(validation_start / 1000, timezone.utc).isoformat() if validation_start else None,
            "inner_train_count": len(inner_train), "validation_count": len(validation),
            "selection": selection,
        }
        if selected_c is None:
            fold_report["reason"] = "C_SELECTION_VALIDATION_INCONCLUSIVE"
            for symbol, side, timeframe in all_groups:
                group_reports.append({"fold": fold["name"], "symbol": symbol,
                    "direction": side, "timeframe": timeframe, "status": "INCONCLUSIVE",
                    "train_count": sum(_group_key(r) == (symbol, side, timeframe) for r in training),
                    "test_count": sum(_group_key(r) == (symbol, side, timeframe) for r in test),
                    "reason": fold_report["reason"]})
            results.append(fold_report)
            continue
        # Threshold is calibrated on validation predictions from a model that
        # has not seen validation labels. Only afterward is the model refit on
        # the complete pre-test training window for OOS scoring.
        validation_model = _fit_pipeline(inner_train, selected_c)
        validation_probabilities = _predict(validation_model, validation)
        threshold = float(np.quantile(validation_probabilities, 2.0 / 3.0))
        model = _fit_pipeline(training, selected_c)
        probabilities = _predict(model, test)
        selected = [p >= threshold for p in probabilities]
        fold_report.update({
            "status": "SCORED", "selected_C": selected_c,
            "take_threshold_from_validation_66_7_percentile": threshold,
            "test_metrics": _classification_metrics(test, probabilities),
            "selected_count": sum(selected),
            "selected_mean_net_r": _mean([r["net_r"] for r, flag in zip(test, selected) if flag]),
            "all_mean_net_r": _mean([r["net_r"] for r in test]),
        })
        fold_report["selected_minus_all_net_r"] = (
            fold_report["selected_mean_net_r"] - fold_report["all_mean_net_r"]
            if fold_report["selected_mean_net_r"] is not None else None
        )
        results.append(fold_report)
        for row, prob, take in zip(test, probabilities, selected):
            oos_predictions.append({**row, "probability": prob, "selected": take, "fold": fold["name"]})
        group_reports.extend(_group_diagnostics(
            test, probabilities, selected, fold["name"], all_groups,
            training_rows=training,
        ))

    scored = [r for r in results if r["status"] == "SCORED"]
    oos_predictions.sort(key=lambda r: (r["signal_time_ms"], r["symbol"], r["side"]))
    oos_rows = [r for r in oos_predictions]
    oos_selected = [r["selected"] for r in oos_predictions]
    all_r = [r["net_r"] for r in oos_rows]
    selected_r = [r["net_r"] for r in oos_rows if r["selected"]]
    uplift = _mean(selected_r) - _mean(all_r) if selected_r else None
    ci = _bootstrap_uplift(oos_rows, oos_selected, reps=bootstrap_reps)
    group_train_gate = all(
        fold.get("status") == "SCORED" or not fold.get("groups_below_min_train")
        for fold in results
    )
    ci_lower = ci.get("ci_95")[0] if ci.get("ci_95") else None
    quality_gates = {
        "three_scored_oos_folds": {"pass": len(scored) >= 3, "observed": len(scored), "required": 3},
        "at_least_100_oos_events": {"pass": len(oos_rows) >= 100, "observed": len(oos_rows), "required": 100},
        "per_group_training_minimum": {"pass": group_train_gate, "required_per_group": min_train_per_group},
        "positive_uplift_ci_lower_bound": {"pass": ci_lower is not None and ci_lower > 0, "observed_lower_bound": ci_lower},
    }
    return {
        "schema_version": 1,
        "verdict": "RESEARCH_DIAGNOSTICS_ONLY_NOT_MODEL_APPROVAL",
        "model": "StandardScaler + LogisticRegression",
        "feature_names": list(FEATURES),
        "feature_alignment": "direction-adjust returns/HTF returns; breakout distance from direction-specific Donchian edge; RSI mirrored for SHORT; log ATR/channel-width/volume; UTC hour cyclic features unchanged",
        "c_grid": list(c_grid), "c_selection_metric": "inner_validation_roc_auc",
        "class_weight": None, "random_state": 31,
        "split_policy": "3 expanding OOS folds; inner chronological 80/20 C selection; 48-bar maturity purge/embargo; scaler fit on training only",
        "feature_timestamp_policy": "REQUIRED: feature_available_time_ms equals closed 15m signal time; 1h/4h close timestamps must be closed UTC boundaries, no later than signal, and no older than one respective timeframe",
        "folds": results, "scored_fold_count": len(scored),
        "oos_event_count": len(oos_rows), "oos_selected_count": len(selected_r),
        "oos_all_mean_net_r": _mean(all_r), "oos_selected_mean_net_r": _mean(selected_r),
        "oos_validation_selected_uplift_net_r": uplift,
        "oos_selected_fraction": (len(selected_r) / len(oos_rows)) if oos_rows else None,
        "oos_uplift_ci_95": ci,
        "group_reports": group_reports, "_oos_predictions": oos_predictions,
        "quality_gates": quality_gates,
        "acceptance": {"status": "NOT_APPROVED_FUNDING_AWARE_PRICE_ONLY_SCOPE",
                       "reasons": [
                           "Funding cashflows are applied per settlement, but retrieval completeness/provenance is not independently verified.",
                           "Maintenance margin, leverage, isolated-wallet evolution, and liquidation are not modeled.",
                           "This report is a Logistic Regression research diagnostic, not model approval or authorization for trading.",
                       ]},
        "runtime": {"numpy": np.__version__, "scikit_learn": sklearn.__version__},
    }


def write_outputs(report: dict, output_dir: Path, source_csv: Path):
    output_dir.mkdir(parents=True, exist_ok=False)
    predictions = report.pop("_oos_predictions", [])
    (output_dir / "direction_aligned_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    fields = ["symbol", "side", "timeframe", "signal_time_ms", "entry_time_ms", "exit_time_ms",
              "label", "net_r", "probability", "selected", "fold", *FEATURES]
    with (output_dir / "oos_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in predictions:
            writer.writerow({**{k: row[k] for k in fields if k in row}, **row["features"]})
    md = ["# Direction-Aligned Research Report", "",
          f"- Verdict: `{report['verdict']}`",
          f"- OOS events: {report['oos_event_count']}",
          f"- Scored folds: {report['scored_fold_count']}/3",
          f"- All OOS mean R: {report['oos_all_mean_net_r']}",
          f"- Selected mean R: {report['oos_selected_mean_net_r']}",
          f"- Validation-selected uplift: {report['oos_validation_selected_uplift_net_r']}",
          f"- 95% CI: {report['oos_uplift_ci_95'].get('ci_95')}", "",
          "## Fold diagnostics", "",
          "| Fold | Status | Train | OOS | AUC | All mean R | Selected mean R | Uplift |",
          "|---|---|---:|---:|---:|---:|---:|---:|"]
    for fold in report["folds"]:
        metrics = fold.get("test_metrics", {})
        md.append("| {fold} | {status} | {train} | {test} | {auc} | {allr} | {selr} | {uplift} |".format(
            fold=fold["fold"], status=fold["status"], train=fold["train_count"], test=fold["test_count"],
            auc=metrics.get("roc_auc"), allr=fold.get("all_mean_net_r"),
            selr=fold.get("selected_mean_net_r"), uplift=fold.get("selected_minus_all_net_r")))
    md.extend(["", "## Symbol × direction × timeframe × period", "",
               "See direction_aligned_report.json → group_reports for the complete group-level table.", "",
               "## Interpretation", "",
               *[f"- {reason}" for reason in report["acceptance"]["reasons"]], ""])
    (output_dir / "direction_aligned_report.md").write_text("\n".join(md), encoding="utf-8")
    manifest = {
        "source_labels_csv": str(source_csv), "source_sha256": sha256_file(source_csv),
        "research_script_sha256": sha256_file(Path(__file__)),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_data_mutated": False, "locked_holdout_accessed": False,
        "source_label_scope": "FUNDING_AWARE_PRICE_ONLY_NO_MARGIN_OR_LIQUIDATION",
        "model": report["model"], "features": report["feature_names"],
        "c_grid": report["c_grid"], "folds": report["folds"],
        "quality_gates": report["quality_gates"],
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path,
                        help="Directory containing labels.csv, research_report.json and dataset_manifest.json")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--c-grid", default=",".join(str(x) for x in C_GRID_DEFAULT))
    parser.add_argument("--min-train-per-group", type=int, default=200)
    parser.add_argument("--min-pooled-train", type=int, default=3000)
    parser.add_argument("--min-test-per-fold", type=int, default=50)
    parser.add_argument("--bootstrap-reps", type=int, default=2000)
    args = parser.parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    labels_csv = input_dir / "labels.csv"
    report_path = input_dir / "research_report.json"
    manifest_path = input_dir / "dataset_manifest.json"
    for required_path in (labels_csv, report_path, manifest_path):
        if not required_path.is_file():
            parser.error(f"required source bundle file not found: {required_path}")
    try:
        source_report = json.loads(report_path.read_text(encoding="utf-8"))
        source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"invalid source bundle metadata: {exc}")
    if (source_report.get("label_scope") != "FUNDING_AWARE_PRICE_ONLY_NO_MARGIN_OR_LIQUIDATION"
            or source_report.get("funding_cashflow_applied") is not True
            or source_report.get("timeframe") != "15m"
            or source_report.get("locked_holdout_months_accessed") is not False
            or source_report.get("model_training_started") is not False
            or source_manifest.get("locked_holdout_accessed") is not False
            or source_manifest.get("model_training_started") is not False):
        parser.error("source bundle is not the frozen Price-only development dataset or holdout/training safety flags failed")
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir == input_dir or input_dir in output_dir.parents:
        parser.error("output directory must be separate from and outside the source label bundle")
    if any(part.lower() == "raw-staging" for part in output_dir.parts):
        parser.error("research outputs must never be written under raw-staging")
    c_grid = tuple(float(item) for item in args.c_grid.split(",") if item.strip())
    if not c_grid or any(not math.isfinite(c) or c <= 0 for c in c_grid):
        parser.error("C grid must contain finite positive values")
    try:
        rows = load_labeled_events(labels_csv)
        report = run_research(rows, c_grid, args.min_train_per_group,
                              args.min_pooled_train, args.min_test_per_fold, args.bootstrap_reps,
                              source_report.get("symbols", FROZEN_SYMBOLS))
        report["source"] = {
            "label_rows": len(rows), "labels_csv_sha256": sha256_file(labels_csv),
            "source_report_sha256": sha256_file(report_path),
            "dataset_manifest_sha256": sha256_file(manifest_path),
            "label_scope": source_report["label_scope"], "timeframe": source_report["timeframe"],
        }
        write_outputs(report, output_dir, labels_csv)
        print(json.dumps({"output_dir": str(output_dir), "verdict": report["verdict"],
                          "oos_events": report["oos_event_count"], "scored_folds": report["scored_fold_count"]}, indent=2))
        return 0
    except Exception as exc:
        parser.exit(2, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
