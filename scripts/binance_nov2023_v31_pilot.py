#!/usr/bin/env python3
"""Research-only Binance BTCUSDT November 2023 Vike V3.1 pilot.

This is a deliberately limited one-symbol/one-month engineering pilot. It
creates Donchian-20 events, causal features, Triple-Barrier labels, applies
explicit illustrative execution costs and actual funding events, and writes
machine-readable diagnostics. It cannot approve a Vike model or replace the
three-year, multi-asset, synchronized walk-forward protocol.

Inputs are immutable raw Binance USD-M archives:
  BTCUSDT-15m-2023-11.zip
  BTCUSDT-15m-YYYY-MM-DD.zip (markPriceKlines, daily)
  funding-rates.json (raw /fapi/v1/fundingRate JSON array)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BAR_MS = 900_000
START_MS = 1698796800000
END_MS = 1701388800000
MISSING_MARK_MS = 1699587900000
SL_ATR = 1.5
TP_ATR = 3.0
MAX_HOLD = 48
FEATURES = ("ret_1", "ret_4", "ret_16", "atr_pct", "donchian_width_pct",
            "channel_position", "volume_ratio", "rsi14", "hour_sin", "hour_cos")
EXIT_REASONS = ("STOP", "TARGET", "TIME_EXIT", "DATA_GAP", "UNMATURED")


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def finite(x, name):
    try:
        value = float(x)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} is not numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} is not finite")
    return value


def parse_zip(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad:
            raise ValueError(f"{path.name}: corrupt ZIP member {bad}")
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{path.name}: expected one CSV, found {len(names)}")
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            if not reader.fieldnames or "open_time" not in reader.fieldnames:
                raise ValueError(f"{path.name}: expected open_time CSV header")
            rows = list(reader)
    return rows


def load_bars(path: Path) -> list[dict]:
    out = []
    for i, row in enumerate(parse_zip(path)):
        t = int(row["open_time"])
        o, h, l, c = (finite(row[k], f"{path.name}[{i}].{k}")
                      for k in ("open", "high", "low", "close"))
        v = finite(row.get("volume", 0), f"{path.name}[{i}].volume")
        if min(o, h, l, c) <= 0 or v < 0 or h < max(o, l, c) or l > min(o, h, c):
            raise ValueError(f"{path.name}: invalid OHLCV at row {i}")
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    out.sort(key=lambda x: x["t"])
    if len({x["t"] for x in out}) != len(out):
        raise ValueError(f"{path.name}: duplicate timestamps")
    if any(b["t"] - a["t"] != BAR_MS for a, b in zip(out, out[1:])):
        raise ValueError(f"{path.name}: internal gap or irregular 15m interval")
    return out


def load_mark(directory: Path) -> tuple[dict[int, dict], list[int], list[str]]:
    files = sorted(directory.glob("BTCUSDT-15m-2023-11-*.zip"))
    if len(files) != 30:
        raise ValueError(f"expected 30 daily mark-price ZIPs, found {len(files)}")
    bars = {}
    hashes = []
    for path in files:
        hashes.append(f"{path.name}:{sha256_file(path)}")
        for row in load_bars(path):
            if row["t"] in bars:
                raise ValueError(f"duplicate mark timestamp {iso(row['t'])}")
            bars[row["t"]] = row
    expected = set(range(START_MS, END_MS, BAR_MS))
    missing = sorted(expected - set(bars))
    extra = sorted(set(bars) - expected)
    return bars, missing, hashes


def wilder_atr(bars: list[dict], end: int, period: int = 14) -> float | None:
    if end < period:
        return None
    trs = []
    for i in range(1, end + 1):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def rsi14(closes: list[float], end: int) -> float | None:
    if end < 14:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(end - 13, end + 1)]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    ag, al = sum(gains) / 14, sum(losses) / 14
    if al == 0:
        return 100.0 if ag else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def build_features(bars: list[dict], i: int) -> dict | None:
    if i < 20 or i < 16:
        return None
    b = bars[i]
    atr = wilder_atr(bars, i)
    if atr is None or atr <= 0:
        return None
    prior = bars[i - 20:i]
    upper, lower = max(x["h"] for x in prior), min(x["l"] for x in prior)
    mean_vol = statistics.fmean(x["v"] for x in bars[i - 20:i])
    closes = [x["c"] for x in bars[:i + 1]]
    hour = (b["t"] // 3_600_000) % 24
    width = upper - lower
    vals = {
        "ret_1": b["c"] / bars[i - 1]["c"] - 1,
        "ret_4": b["c"] / bars[i - 4]["c"] - 1,
        "ret_16": b["c"] / bars[i - 16]["c"] - 1,
        "atr_pct": atr / b["c"],
        "donchian_width_pct": width / b["c"],
        "channel_position": (b["c"] - lower) / width if width else 0.5,
        "volume_ratio": b["v"] / mean_vol if mean_vol > 0 else 0.0,
        "rsi14": (rsi14(closes, i) or 50.0) / 100.0,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
    }
    if not all(math.isfinite(v) for v in vals.values()):
        return None
    return vals


def generate_events(bars: list[dict]) -> list[dict]:
    events, armed = [], {"LONG": True, "SHORT": True}
    for i in range(20, len(bars) - 1):
        prior = bars[i - 20:i]
        upper, lower = max(x["h"] for x in prior), min(x["l"] for x in prior)
        close = bars[i]["c"]
        # Fresh re-entry inside the prior channel re-arms that direction.
        if close <= upper:
            armed["LONG"] = True
        if close >= lower:
            armed["SHORT"] = True
        side = "LONG" if close > upper else "SHORT" if close < lower else None
        if side and armed[side]:
            features = build_features(bars, i)
            if features is not None:
                events.append({
                    "event_index": len(events), "signal_index": i,
                    "signal_time_ms": bars[i]["t"] + BAR_MS,
                    "entry_index": i + 1, "entry_time_ms": bars[i + 1]["t"],
                    "side": side, "signal_close": close,
                    "atr14": wilder_atr(bars, i), "features": features,
                })
            armed[side] = False
    return events


def load_funding(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    rows = json.loads(raw.decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("funding JSON must be a list")
    out, seen = [], set()
    for i, row in enumerate(rows):
        if row.get("symbol") != "BTCUSDT":
            raise ValueError(f"funding row {i}: unexpected symbol")
        t = int(row["fundingTime"])
        rate = finite(row["fundingRate"], f"funding[{i}].fundingRate")
        if t in seen:
            raise ValueError(f"duplicate fundingTime {t}")
        seen.add(t)
        if not START_MS <= t < END_MS:
            raise ValueError(f"funding timestamp outside November window: {t}")
        out.append({"t": t, "rate": rate})
    return sorted(out, key=lambda x: x["t"]), hashlib.sha256(raw).hexdigest()


def label_event(event: dict, bars: list[dict], mark: dict[int, dict],
                missing_mark: set[int], funding: list[dict],
                fee_bps: float, slippage_bps: float) -> dict:
    entry_i = event["entry_index"]
    if entry_i + MAX_HOLD >= len(bars):
        return {"exit_reason": "UNMATURED", "eligible": False}
    entry = bars[entry_i]["o"]
    side = event["side"]
    sign = 1 if side == "LONG" else -1
    atr = event["atr14"]
    stop = entry - sign * SL_ATR * atr
    target = entry + sign * TP_ATR * atr
    exit_i, exit_px, reason = None, None, None
    last_i = min(entry_i + MAX_HOLD, len(bars) - 1)
    for j in range(entry_i, last_i + 1):
        bar = bars[j]
        if j > entry_i and bar["t"] - bars[j - 1]["t"] != BAR_MS:
            return {"exit_reason": "DATA_GAP", "eligible": False}
        stop_hit = bar["l"] <= stop if side == "LONG" else bar["h"] >= stop
        target_hit = bar["h"] >= target if side == "LONG" else bar["l"] <= target
        if stop_hit:
            exit_i, exit_px, reason = j, min(bar["o"], stop) if side == "LONG" else max(bar["o"], stop), "STOP"
            break
        if target_hit:
            exit_i, exit_px, reason = j, max(bar["o"], target) if side == "LONG" else min(bar["o"], target), "TARGET"
            break
    if exit_i is None:
        exit_i, exit_px, reason = last_i, bars[last_i]["c"], "TIME_EXIT"
    entry_t, exit_t = bars[entry_i]["t"], bars[exit_i]["t"] + BAR_MS
    # Mark-price completeness is a fail-closed requirement for the entire
    # holding interval. No interpolation or forward-fill is permitted.
    held_mark_times = range(entry_t, exit_t, BAR_MS)
    if any(t not in mark for t in held_mark_times):
        return {"exit_reason": "DATA_GAP", "eligible": False,
                "exclusion": "MISSING_MARK_PRICE_DURING_HOLD"}
    gross = sign * (exit_px - entry) / entry
    fee = 2.0 * fee_bps / 10_000.0
    slip = 2.0 * slippage_bps / 10_000.0
    funding_cost = 0.0
    for item in funding:
        if entry_t <= item["t"] < exit_t:
            funding_cost += sign * item["rate"] * (entry / entry)
    net_return = gross - fee - slip - funding_cost
    initial_risk_pct = SL_ATR * atr / entry
    net_r = net_return / initial_risk_pct
    cost_r = (fee + slip + funding_cost) / initial_risk_pct
    return {
        "exit_reason": reason, "eligible": True,
        "entry_price": entry, "exit_price": exit_px,
        "entry_time_utc": iso(entry_t), "exit_time_utc": iso(exit_t),
        "gross_return_pct": gross, "fee_pct": fee, "slippage_pct": slip,
        "funding_cost_pct": funding_cost, "net_return_pct": net_return,
        "initial_stop_distance_pct": initial_risk_pct,
        "net_r": net_r, "cost_to_r": cost_r,
        "label": int(net_r > 0),
    }


def fit_logistic(rows: list[dict], iterations: int = 2500, lr: float = 0.08):
    """Small deterministic L2 logistic regression using standardized features."""
    if len(rows) < 30 or len({r["label"] for r in rows}) < 2:
        return None, {"status": "NOT_FIT", "reason": "INSUFFICIENT_ROWS_OR_SINGLE_CLASS"}
    means = [statistics.fmean(r["features"][k] for r in rows) for k in FEATURES]
    scales = []
    for j, key in enumerate(FEATURES):
        variance = statistics.fmean((r["features"][key] - means[j]) ** 2 for r in rows)
        scales.append(math.sqrt(variance) or 1.0)
    xs = [[(r["features"][k] - means[j]) / scales[j] for j, k in enumerate(FEATURES)] for r in rows]
    ys = [r["label"] for r in rows]
    weights = [0.0] * len(FEATURES)
    bias = 0.0
    n = len(rows)
    for step in range(iterations):
        grad = [0.0] * len(FEATURES)
        gb = 0.0
        for x, y in zip(xs, ys):
            z = max(-30.0, min(30.0, bias + sum(a * b for a, b in zip(weights, x))))
            p = 1.0 / (1.0 + math.exp(-z))
            error = p - y
            gb += error
            for j, value in enumerate(x):
                grad[j] += error * value
        rate = lr / math.sqrt(1.0 + step / 100.0)
        bias -= rate * gb / n
        for j in range(len(weights)):
            weights[j] -= rate * (grad[j] / n + 0.01 * weights[j])
    return {"means": means, "scales": scales, "weights": weights, "bias": bias}, {
        "status": "FIT", "training_rows": n, "positive_rate": statistics.fmean(ys),
        "iterations": iterations, "l2_alpha": 0.01,
    }


def predict(model: dict, features: dict) -> float:
    x = [(features[k] - model["means"][j]) / model["scales"][j]
         for j, k in enumerate(FEATURES)]
    z = max(-30.0, min(30.0, model["bias"] + sum(a * b for a, b in zip(model["weights"], x))))
    return 1.0 / (1.0 + math.exp(-z))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trade-zip", required=True)
    p.add_argument("--mark-dir", required=True)
    p.add_argument("--funding-json", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    args = p.parse_args()
    trade_path, mark_dir, funding_path, out_dir = map(Path, (
        args.trade_zip, args.mark_dir, args.funding_json, args.out_dir))
    if args.fee_bps_per_side < 0 or args.slippage_bps_per_side < 0:
        raise ValueError("fee and slippage assumptions must be non-negative")
    out_dir.mkdir(parents=True, exist_ok=True)
    bars = load_bars(trade_path)
    expected = list(range(START_MS, END_MS, BAR_MS))
    if [b["t"] for b in bars] != expected:
        raise ValueError("trade candles do not exactly cover the registered November window")
    mark, missing, mark_hashes = load_mark(mark_dir)
    funding, funding_hash = load_funding(funding_path)
    events = generate_events(bars)
    labelled = []
    for event in events:
        outcome = label_event(event, bars, mark, set(missing), funding,
                              args.fee_bps_per_side, args.slippage_bps_per_side)
        labelled.append({**event, **outcome})
    eligible = [r for r in labelled if r.get("eligible")]
    # Strictly chronological diagnostic split only. It is not the protocol's
    # three-year/30-month WFO plus locked six-month holdout.
    eligible.sort(key=lambda r: r["signal_time_ms"])
    cut = int(len(eligible) * 0.70)
    train, test = eligible[:cut], eligible[cut:]
    model, fit_report = fit_logistic(train)
    predictions = []
    if model and test:
        predictions = [{"event_index": r["event_index"], "probability": predict(model, r["features"]),
                        "label": r["label"], "net_r": r["net_r"]} for r in test]
    costs = [r["cost_to_r"] for r in eligible]
    net_rs = [r["net_r"] for r in eligible]
    reasons = {}
    for row in labelled:
        reason = row.get("exclusion") or row.get("exit_reason", "UNKNOWN")
        reasons[reason] = reasons.get(reason, 0) + 1
    hashes = {"trade_zip": sha256_file(trade_path), "funding_json": funding_hash,
              "mark_archives": mark_hashes}
    dataset_manifest = {
        "experiment": "BINANCE_USDM_BTCUSDT_15M_2023_11_PILOT",
        "research_only": True, "runtime_eligible": False,
        "data_start_utc": iso(START_MS), "data_end_exclusive_utc": iso(END_MS),
        "symbol": "BTCUSDT", "venue": "Binance USD-M", "timeframe": "15m",
        "source_hashes": hashes,
        "event_config": {"donchian_period": 20, "fresh_reentry_required": True,
                         "entry": "next_15m_open"},
        "label_config": {"atr": "Wilder ATR14 through signal close",
                         "stop_atr": SL_ATR, "target_atr": TP_ATR,
                         "max_holding_bars": MAX_HOLD, "same_bar_priority": "STOP_FIRST"},
        "execution_assumptions": {"taker_fee_bps_per_side": args.fee_bps_per_side,
                                  "adverse_slippage_bps_per_side": args.slippage_bps_per_side,
                                  "funding": "actual historical settlement timestamps",
                                  "mark_price": "all holding bars required; missing bars excluded"},
        "features": list(FEATURES),
    }
    report = {
        "report_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": "PILOT_DIAGNOSTIC_ONLY_NOT_MODEL_APPROVAL",
        "training_started": bool(model), "model_fit": fit_report,
        "data_quality": {"trade_rows": len(bars), "mark_rows": len(mark),
                         "mark_missing_count": len(missing),
                         "mark_missing_timestamps_utc": [iso(x) for x in missing],
                         "mark_extra_count": sum(1 for t in mark if not START_MS <= t < END_MS),
                         "funding_rows": len(funding)},
        "counts": {"events": len(events), "eligible_labels": len(eligible),
                   "train_rows": len(train), "chronological_test_rows": len(test),
                   "test_predictions": len(predictions), "positive_labels": sum(r["label"] for r in eligible)},
        "exclusions_by_reason": reasons,
        "metrics": {"median_cost_to_r": statistics.median(costs) if costs else None,
                    "p90_cost_to_r": sorted(costs)[min(len(costs)-1, math.ceil(.9*len(costs))-1)] if costs else None,
                    "mean_net_r": statistics.fmean(net_rs) if net_rs else None,
                    "profit_factor": (sum(x for x in net_rs if x > 0) / abs(sum(x for x in net_rs if x < 0)))
                        if any(x < 0 for x in net_rs) else None},
        "protocol_gates": {
            "three_years": False, "five_to_eight_point_in_time_symbols": False,
            "three_thousand_matured_overlap_controlled_samples": False,
            "three_walk_forward_folds": False, "two_hundred_wf_oos_signals": False,
            "two_hundred_locked_holdout_signals": False,
            "synchronized_block_bootstrap_10000": False,
            "reason": "One month and one symbol are an engineering pilot only; final V3.1 gates remain unchanged."
        },
        "dataset_manifest": dataset_manifest,
        "test_predictions": predictions,
        "events": [{k: v for k, v in r.items() if k != "features"} for r in labelled],
    }
    (out_dir / "dataset_manifest.json").write_text(json.dumps(dataset_manifest, indent=2) + "\n", encoding="utf-8")
    (out_dir / "pilot_report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out_dir / "events.json").write_text(json.dumps(labelled, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(out_dir / "pilot_report.json"),
                      "events": len(events), "eligible": len(eligible),
                      "training_started": bool(model),
                      "verdict": report["verdict"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
