#!/usr/bin/env python3
"""Read-only Vike V3.1 exploratory baseline trainer.

Reads existing monthly Binance USD-M CSVs and writes only beneath
derived/exploratory-training. It never downloads, edits, moves, or deletes raw
files. Labels are PROVISIONAL fixed-horizon net-return proxies, not Triple-
Barrier labels and never eligible for model acceptance.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math, statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

BAR_MS, DAY_MS, HOUR_MS = 900_000, 86_400_000, 3_600_000
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT")
MONTHS = tuple(f"{y:04d}-{m:02d}" for y in range(2023, 2027) for m in range(1, 13)
               if (y, m) >= (2023, 11) and (y, m) <= (2026, 4))
FEATURES = ("ret_1", "ret_4", "ret_16", "atr_pct", "donchian_width_pct",
            "channel_position", "volume_ratio", "rsi14", "hour_sin", "hour_cos",
            "htf_1h_ret_1", "htf_1h_ret_4", "htf_4h_ret_1", "htf_4h_ret_4")
STREAM_NAMES = {"trade_15m": "trade_15m.csv", "mark_15m": "mark_15m.csv", "funding": "funding.csv"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def month_bounds(month: str) -> tuple[int, int]:
    dt = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    end = dt.replace(year=dt.year + 1, month=1) if dt.month == 12 else dt.replace(month=dt.month + 1)
    return int(dt.timestamp() * 1000), int(end.timestamp() * 1000)


def resolve_csv(raw_root: Path, month: str, symbol: str, stream: str) -> Path:
    filename, month_dir = STREAM_NAMES[stream], raw_root / month
    candidates = (month_dir / symbol / filename, month_dir / symbol / f"{symbol}-{filename}",
                  month_dir / f"{symbol}-{filename}", month_dir / symbol / f"{stream}.csv")
    found = {p.resolve() for p in candidates if p.is_file()}
    if not found and month_dir.is_dir():
        for p in month_dir.rglob("*.csv"):
            if p.name.lower() in (filename.lower(), f"{symbol}-{filename}".lower()):
                if symbol in {part.upper() for part in p.parts} or p.name.upper().startswith(symbol + "-"):
                    found.add(p.resolve())
    if len(found) != 1:
        raise FileNotFoundError(f"{month}/{symbol}/{stream}: expected exactly one {filename}; found {len(found)}")
    return next(iter(found))


def read_trade_or_mark(path: Path, stream: str, start: int, end: int) -> dict[int, dict]:
    keys = ("open", "high", "low", "close", "volume") if stream == "trade_15m" else (
        "mark_open", "mark_high", "mark_low", "mark_close")
    out = {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"open_time", "close_time", *keys}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path}: missing required columns {sorted(required - set(reader.fieldnames or []))}")
        for line, row in enumerate(reader, 2):
            try:
                t, close_t = int(row["open_time"]), int(row["close_time"])
                values = [float(row[k]) for k in keys]
                if t in out: raise ValueError("duplicate timestamp")
                if not start <= t < end or t % BAR_MS or close_t != t + BAR_MS - 1:
                    raise ValueError("timestamp outside month or misaligned candle")
                if not all(math.isfinite(x) for x in values): raise ValueError("non-finite OHLCV")
                o, h, l, c = values[:4]
                if min(o, h, l, c) <= 0 or h < max(o, l, c) or l > min(o, c):
                    raise ValueError("invalid OHLC")
                volume = values[4] if stream == "trade_15m" else 0.0
                if volume < 0: raise ValueError("negative volume")
                out[t] = {"t": t, "close_t": close_t, "o": o, "h": h, "l": l, "c": c, "v": volume}
            except Exception as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
    return out


def audit_funding_file(path: Path, start: int, end: int) -> dict:
    count, previous = 0, None
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"funding_time", "funding_rate", "mark_price"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path}: funding schema missing required columns")
        for line, row in enumerate(reader, 2):
            t, rate, mark = int(row["funding_time"]), float(row["funding_rate"]), float(row["mark_price"])
            if not start <= t < end or not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0:
                raise ValueError(f"{path}:{line}: invalid funding record")
            if previous is not None and t <= previous:
                raise ValueError(f"{path}:{line}: funding timestamps not strictly increasing")
            previous, count = t, count + 1
    return {"rows": count, "structural_status": "PASS" if count else "FAIL", "coverage_status": "NOT_VERIFIED",
            "used_as_feature_or_label_input": False}


def complete_day_bars(trade: dict[int, dict], mark: dict[int, dict], start: int, end: int):
    eligible, excluded = [], []
    for day_start in range(start, end, DAY_MS):
        day_end = day_start + DAY_MS
        expected = set(range(day_start, day_end, BAR_MS))
        ts = {t for t in trade if day_start <= t < day_end}
        ms = {t for t in mark if day_start <= t < day_end}
        if ts == expected and ms == expected:
            eligible.extend(trade[t] for t in sorted(expected))
        else:
            excluded.append({"date_utc": datetime.fromtimestamp(day_start / 1000, timezone.utc).date().isoformat(),
                             "trade_bars": len(ts), "mark_bars": len(ms),
                             "reason": "INCOMPLETE_OR_MISALIGNED_PRICE_DAY"})
    return eligible, excluded


def split_contiguous(bars: list[dict]) -> list[list[dict]]:
    if not bars: return []
    segments, current = [], [bars[0]]
    for bar in bars[1:]:
        if bar["t"] != current[-1]["t"] + BAR_MS:
            segments.append(current)
            current = [bar]
        else: current.append(bar)
    segments.append(current)
    return segments


def aggregate_context(bars: list[dict], interval_ms: int, expected_count: int):
    buckets = defaultdict(list)
    for bar in bars: buckets[(bar["t"] // interval_ms) * interval_ms].append(bar)
    closes, values = [], []
    for start, rows in sorted(buckets.items()):
        rows.sort(key=lambda x: x["t"])
        expected = list(range(start, start + interval_ms, BAR_MS))
        if len(rows) == expected_count and [x["t"] for x in rows] == expected:
            closes.append(start + interval_ms - 1)
            values.append(rows[-1]["c"])
    return closes, values


def context_returns(closes: list[int], values: list[float], event_close_ms: int):
    i = bisect_right(closes, event_close_ms) - 1
    if i < 4 or values[i - 4] <= 0: return 0.0, 0.0
    return values[i] / values[i - 1] - 1.0, values[i] / values[i - 4] - 1.0


def wilder_atr_values(bars: list[dict], period: int = 14):
    result, trs, previous_atr = [None] * len(bars), [], None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(
            b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        trs.append(tr)
        if i == period - 1:
            previous_atr = sum(trs[:period]) / period
            result[i] = previous_atr
        elif i >= period:
            previous_atr = (previous_atr * (period - 1) + tr) / period
            result[i] = previous_atr
    return result


def rsi14(bars: list[dict], i: int) -> float:
    changes = [bars[j]["c"] - bars[j - 1]["c"] for j in range(i - 13, i + 1)]
    gains, losses = [max(x, 0.0) for x in changes], [max(-x, 0.0) for x in changes]
    ag, al = sum(gains) / 14, sum(losses) / 14
    return 1.0 if al == 0 and ag else 0.5 if al == 0 else 1.0 - 1.0 / (1.0 + ag / al)


def build_segment_events(symbol: str, bars: list[dict], mark_bars: list[dict],
                         fee_rate: float, slippage_rate: float, horizon: int):
    if len(bars) < horizon + 21: return []
    atrs = wilder_atr_values(bars)
    h1c, h1v = aggregate_context(bars, HOUR_MS, 4)
    h4c, h4v = aggregate_context(bars, 4 * HOUR_MS, 16)
    mark_times = {b["t"] for b in mark_bars}
    events, armed = [], {"LONG": True, "SHORT": True}
    for i in range(20, len(bars) - horizon):
        b, prior = bars[i], bars[i - 20:i]
        upper, lower = max(x["h"] for x in prior), min(x["l"] for x in prior)
        if lower <= b["c"] <= upper: armed = {"LONG": True, "SHORT": True}
        side = "LONG" if b["c"] > upper else "SHORT" if b["c"] < lower else None
        if side is None or not armed[side] or atrs[i] is None or atrs[i] <= 0: continue
        entry_bar, exit_bar = bars[i + 1], bars[i + horizon]
        held_times = [bars[j]["t"] for j in range(i + 1, i + horizon + 1)]
        if any(t not in mark_times for t in held_times):
            armed[side] = False
            continue
        entry, exit_price = entry_bar["o"], exit_bar["c"]
        direction = 1.0 if side == "LONG" else -1.0
        net_return = direction * (exit_price - entry) / entry - 2.0 * fee_rate - 2.0 * slippage_rate
        risk_fraction = 1.5 * atrs[i] / entry
        if risk_fraction <= 0 or not math.isfinite(net_return):
            armed[side] = False
            continue
        signal_close = b["close_t"]
        h1r1, h1r4 = context_returns(h1c, h1v, signal_close)
        h4r1, h4r4 = context_returns(h4c, h4v, signal_close)
        hour, width = (b["t"] // HOUR_MS) % 24, upper - lower
        mean_vol = statistics.fmean(x["v"] for x in prior)
        f = {
            "ret_1": b["c"] / bars[i - 1]["c"] - 1.0,
            "ret_4": b["c"] / bars[i - 4]["c"] - 1.0,
            "ret_16": b["c"] / bars[i - 16]["c"] - 1.0,
            "atr_pct": atrs[i] / b["c"], "donchian_width_pct": width / b["c"] if width else 0.0,
            "channel_position": (b["c"] - lower) / width if width else 0.5,
            "volume_ratio": b["v"] / mean_vol if mean_vol > 0 else 0.0, "rsi14": rsi14(bars, i),
            "hour_sin": math.sin(2 * math.pi * hour / 24.0), "hour_cos": math.cos(2 * math.pi * hour / 24.0),
            "htf_1h_ret_1": h1r1, "htf_1h_ret_4": h1r4, "htf_4h_ret_1": h4r1, "htf_4h_ret_4": h4r4,
        }
        if not all(math.isfinite(v) for v in f.values()):
            armed[side] = False
            continue
        events.append({"symbol": symbol, "signal_time_ms": signal_close, "entry_time_ms": entry_bar["t"],
                       "exit_time_ms": exit_bar["close_t"], "side": side, "label": int(net_return > 0),
                       "net_return": net_return, "net_r_proxy": net_return / risk_fraction,
                       "label_status": "PROVISIONAL_FIXED_HORIZON_PROXY", "features": f})
        armed[side] = False
    return events


def load_dataset(drive_root: Path, symbols: Iterable[str], months: Iterable[str],
                 fee_bps: float, slippage_bps: float, horizon: int):
    raw_root = drive_root / "raw-staging"
    if not raw_root.is_dir(): raise FileNotFoundError(f"raw-staging directory not found: {raw_root}")
    all_events, monthly, exclusions, file_hashes = [], [], [], []
    fee_rate, slip_rate = fee_bps / 10_000.0, slippage_bps / 10_000.0
    for symbol in symbols:
        symbol_bars, symbol_marks = {}, {}
        for month in months:
            start, end = month_bounds(month)
            files = {stream: resolve_csv(raw_root, month, symbol, stream) for stream in STREAM_NAMES}
            for stream, path in files.items():
                file_hashes.append({"symbol": symbol, "month": month, "stream": stream,
                                    "path_relative": str(path.relative_to(drive_root)),
                                    "sha256": sha256_file(path), "bytes": path.stat().st_size})
            trade = read_trade_or_mark(files["trade_15m"], "trade_15m", start, end)
            mark = read_trade_or_mark(files["mark_15m"], "mark_15m", start, end)
            funding = audit_funding_file(files["funding"], start, end)
            if funding["structural_status"] != "PASS":
                raise ValueError(f"{symbol}/{month}: Funding file has no valid rows")
            eligible, excluded = complete_day_bars(trade, mark, start, end)
            monthly.append({"symbol": symbol, "month": month, "trade_rows": len(trade),
                            "mark_rows": len(mark), "price_eligible_days": len(eligible) // 96,
                            "excluded_days": excluded, "funding": funding})
            exclusions.extend({"symbol": symbol, "month": month, **x} for x in excluded)
            for bar in eligible:
                symbol_bars[bar["t"]], symbol_marks[bar["t"]] = bar, mark[bar["t"]]
        ordered = [symbol_bars[t] for t in sorted(symbol_bars)]
        for segment in split_contiguous(ordered):
            marks = [symbol_marks[b["t"]] for b in segment]
            all_events.extend(build_segment_events(symbol, segment, marks, fee_rate, slip_rate, horizon))
    return all_events, monthly, exclusions, file_hashes


def train_and_report(events: list[dict], output_dir: Path, metadata: dict):
    # Keep research-only ML dependencies lazy so data-loader tests can run in the base test environment.
    import joblib
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, brier_score_loss, precision_score, recall_score, roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    if len(events) < 60: raise ValueError(f"Only {len(events)} eligible events; at least 60 required for exploratory split")
    events.sort(key=lambda x: x["signal_time_ms"])
    split_index = int(len(events) * 0.70)
    test_start = events[split_index]["signal_time_ms"]
    train = [e for e in events[:split_index] if e["exit_time_ms"] < test_start]
    test = events[split_index:]
    if len(train) < 30 or len({e["label"] for e in train}) < 2:
        raise ValueError("Insufficient two-class purged training data for exploratory Logistic Regression")
    x_train = np.asarray([[e["features"][k] for k in FEATURES] for e in train], dtype=float)
    y_train = np.asarray([e["label"] for e in train], dtype=int)
    x_test = np.asarray([[e["features"][k] for k in FEATURES] for e in test], dtype=float)
    y_test = np.asarray([e["label"] for e in test], dtype=int)
    model = Pipeline([("scale", StandardScaler()),
                      ("logistic", LogisticRegression(C=0.1, max_iter=2000, class_weight="balanced", random_state=31))])
    model.fit(x_train, y_train)
    probabilities = model.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)
    metrics = {"accuracy": float(accuracy_score(y_test, predictions)),
               "precision": float(precision_score(y_test, predictions, zero_division=0)),
               "recall": float(recall_score(y_test, predictions, zero_division=0)),
               "brier": float(brier_score_loss(y_test, probabilities)),
               "roc_auc": float(roc_auc_score(y_test, probabilities)) if len(set(y_test)) == 2 else None,
               "mean_test_net_r_proxy_all_events": float(statistics.fmean(e["net_r_proxy"] for e in test)),
               "mean_test_net_r_proxy_predicted_take": float(statistics.fmean(
                   e["net_r_proxy"] for e, pred in zip(test, predictions) if pred == 1)) if np.any(predictions == 1) else None}
    model_path = output_dir / "provisional_logistic_regression.joblib"
    joblib.dump(model, model_path)
    report = {"verdict": "EXPLORATORY_ONLY_NOT_MODEL_APPROVAL", "training_started": True,
              "label_policy": "PROVISIONAL_FIXED_HORIZON_48_BAR_NET_RETURN_PROXY",
              "label_definition": "next-bar-open to 48th-bar-close net return after explicit round-trip fee/slippage",
              "triple_barrier_used": False, "funding_coverage_status": "NOT_VERIFIED",
              "funding_used_in_labels_or_features": False, "liquidation_verification_status": "NOT_VERIFIED",
              "liquidation_used_in_labels": False, "walk_forward_acceptance": "NOT_RUN",
              "locked_holdout_accessed": False, "event_count": len(events),
              "train_count_after_purge": len(train), "test_count": len(test),
              "test_start_utc": datetime.fromtimestamp(test_start / 1000, timezone.utc).isoformat(),
              "class_balance_train": {"negative": int((y_train == 0).sum()), "positive": int((y_train == 1).sum())},
              "class_balance_test": {"negative": int((y_test == 0).sum()), "positive": int((y_test == 1).sum())},
              "metrics": metrics, "model_artifact": model_path.name, "model_sha256": sha256_file(model_path),
              "feature_names": list(FEATURES), "metadata": metadata}
    with (output_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["symbol", "signal_time_ms", "entry_time_ms", "exit_time_ms", "side", "label",
                  "net_return", "net_r_proxy", "label_status", *FEATURES]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for e in events: writer.writerow({**{k: e[k] for k in fields if k in e}, **e["features"]})
    (output_dir / "exploratory_report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "excluded_days.json").write_text(json.dumps(metadata["excluded_days"], indent=2) + "\n", encoding="utf-8")
    return report


def run(args):
    drive_root = Path(args.drive_root).expanduser().resolve()
    raw_root = (drive_root / "raw-staging").resolve()
    output_root = (drive_root / "derived" / "exploratory-training").resolve()
    if raw_root in output_root.parents or output_root == raw_root:
        raise ValueError("Output must remain outside raw-staging")
    if not raw_root.is_dir(): raise FileNotFoundError(f"Raw data directory missing: {raw_root}")
    if args.fee_bps_per_side < 0 or args.slippage_bps_per_side < 0:
        raise ValueError("fee and slippage assumptions must be nonnegative")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in SYMBOLS for s in symbols):
        raise ValueError("symbols must be a unique subset of the frozen eight-symbol universe")
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=False, exist_ok=False)
    events, monthly, exclusions, hashes = load_dataset(
        drive_root, symbols, MONTHS, args.fee_bps_per_side, args.slippage_bps_per_side, args.horizon_bars)
    metadata = {"run_id": run_id, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "venue": "Binance USD-M", "market_type": "USDT perpetual futures", "timeframe": "15m",
                "symbols": symbols, "months": list(MONTHS), "development_window_only": True,
                "locked_holdout_months": ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"],
                "fee_bps_per_side": args.fee_bps_per_side, "slippage_bps_per_side": args.slippage_bps_per_side,
                "horizon_bars": args.horizon_bars, "funding_coverage_status": "NOT_VERIFIED",
                "file_hashes": hashes, "monthly_audits": monthly, "excluded_days": exclusions,
                "raw_data_mutated": False}
    report = train_and_report(events, output_dir, metadata)
    (output_dir / "dataset_manifest.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return {"output_dir": str(output_dir), "report": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive-root", required=True)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--fee-bps-per-side", type=float, default=5.0)
    parser.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    parser.add_argument("--horizon-bars", type=int, default=48)
    args = parser.parse_args()
    if args.horizon_bars < 1 or args.horizon_bars > 48: parser.error("--horizon-bars must be between 1 and 48")
    try:
        result = run(args)
        r = result["report"]
        print(json.dumps({"output_dir": result["output_dir"], "verdict": r["verdict"],
                           "events": r["event_count"], "train": r["train_count_after_purge"],
                           "test": r["test_count"]}, indent=2))
        return 0
    except Exception as exc:
        parser.exit(2, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
