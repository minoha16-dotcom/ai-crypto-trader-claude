"""Fail-closed, diagnostic-only Vike direction-aligned model inference.

This module has no exchange, scanner, paper-trade, or order-execution imports.
The caller supplies a feature vector produced by a separately validated adapter.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

FEATURE_NAMES = (
    "dir_ret_1", "dir_ret_4", "dir_ret_16",
    "log_atr_pct", "log_donchian_width_pct", "dir_breakout_distance",
    "log_volume_ratio", "dir_rsi14", "hour_sin", "hour_cos",
    "dir_htf_1h_ret_1", "dir_htf_1h_ret_4",
    "dir_htf_4h_ret_1", "dir_htf_4h_ret_4",
)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT")
BAR_MS = 15 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
FOUR_HOUR_MS = 4 * HOUR_MS


class ShadowModelError(ValueError):
    """Artifact or inference input is incompatible or unsafe."""


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ShadowModelError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ShadowModelError(f"{name} must be finite")
    return number


def validate_causal_timestamps(signal_time_ms: int, feature_available_time_ms: int,
                               htf_1h_close_time_ms: int, htf_4h_close_time_ms: int) -> None:
    values = (signal_time_ms, feature_available_time_ms, htf_1h_close_time_ms, htf_4h_close_time_ms)
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        raise ShadowModelError("all feature timestamps must be integer milliseconds")
    if signal_time_ms % BAR_MS != BAR_MS - 1:
        raise ShadowModelError("signal_time_ms must be a closed UTC 15m candle boundary")
    if feature_available_time_ms != signal_time_ms:
        raise ShadowModelError("feature_available_time_ms must equal closed signal timestamp")
    if htf_1h_close_time_ms > signal_time_ms or htf_4h_close_time_ms > signal_time_ms:
        raise ShadowModelError("future higher-timeframe feature timestamp")
    if htf_1h_close_time_ms % HOUR_MS != HOUR_MS - 1:
        raise ShadowModelError("1h context timestamp is not a closed UTC hour boundary")
    if htf_4h_close_time_ms % FOUR_HOUR_MS != FOUR_HOUR_MS - 1:
        raise ShadowModelError("4h context timestamp is not a closed UTC 4h boundary")
    if signal_time_ms - htf_1h_close_time_ms >= HOUR_MS:
        raise ShadowModelError("1h context is stale or incomplete")
    if signal_time_ms - htf_4h_close_time_ms >= FOUR_HOUR_MS:
        raise ShadowModelError("4h context is stale or incomplete")



RAW_FEATURE_NAMES = (
    "ret_1", "ret_4", "ret_16", "atr_pct", "donchian_width_pct",
    "channel_position", "volume_ratio", "rsi14", "hour_sin", "hour_cos",
    "htf_1h_ret_1", "htf_1h_ret_4", "htf_4h_ret_1", "htf_4h_ret_4",
)


def direction_aligned_features(raw_features: dict[str, Any], side: str) -> dict[str, float]:
    """Apply the exact raw-to-direction-aligned transform used by Vike research."""
    if not isinstance(raw_features, dict) or set(raw_features) != set(RAW_FEATURE_NAMES):
        raise ShadowModelError("raw feature schema mismatch")
    side = str(side).upper()
    if side not in ("LONG", "SHORT"):
        raise ShadowModelError("side must be LONG or SHORT")
    raw = {name: _finite_number(raw_features[name], name) for name in RAW_FEATURE_NAMES}
    if raw["atr_pct"] <= 0 or raw["donchian_width_pct"] <= 0 or raw["volume_ratio"] < 0:
        raise ShadowModelError("ATR/channel width must be positive and volume ratio nonnegative")
    if not 0.0 <= raw["rsi14"] <= 1.0:
        raise ShadowModelError("rsi14 must be normalized to [0, 1]")
    sign = 1.0 if side == "LONG" else -1.0
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
    validate_features(result)
    return result

def validate_features(features: dict[str, Any]) -> list[float]:
    if not isinstance(features, dict):
        raise ShadowModelError("features must be an object")
    expected, actual = set(FEATURE_NAMES), set(features)
    if actual != expected:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise ShadowModelError(f"feature schema mismatch; missing={missing}, extra={extra}")
    return [_finite_number(features[name], name) for name in FEATURE_NAMES]


def load_artifact(path: str | Path) -> tuple[dict, str]:
    """Load a JSON artifact with an explicit, reviewed, non-pickle contract."""
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise ShadowModelError("shadow model artifact is missing")
    try:
        raw_bytes = candidate.read_bytes()
    except OSError as exc:
        raise ShadowModelError("shadow model artifact cannot be read") from exc
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        artifact = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowModelError("artifact must be valid UTF-8 JSON") from exc
    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
        raise ShadowModelError("unsupported artifact schema")
    if artifact.get("artifact_status") != "SHADOW_ONLY":
        raise ShadowModelError("artifact is not explicitly marked SHADOW_ONLY")
    if artifact.get("model_type") != "logistic_regression":
        raise ShadowModelError("only the reviewed logistic regression artifact is supported")
    if artifact.get("feature_names") != list(FEATURE_NAMES):
        raise ShadowModelError("artifact feature order does not match runtime schema")
    if artifact.get("market_scope") != "BINANCE_USDM_PERPETUAL" or artifact.get("timeframe") != "15m":
        raise ShadowModelError("artifact market/timeframe scope mismatch")
    if not isinstance(artifact.get("model_version"), str) or not artifact["model_version"].strip():
        raise ShadowModelError("artifact model_version is required")
    scaler, logistic = artifact.get("standard_scaler"), artifact.get("logistic_regression")
    if not isinstance(scaler, dict) or not isinstance(logistic, dict):
        raise ShadowModelError("artifact scaler/logistic parameters are missing")
    for key in ("mean", "scale"):
        values = scaler.get(key)
        if not isinstance(values, list) or len(values) != len(FEATURE_NAMES):
            raise ShadowModelError(f"scaler {key} must have {len(FEATURE_NAMES)} values")
        parsed = [_finite_number(v, f"scaler.{key}") for v in values]
        if key == "scale" and any(v <= 0 for v in parsed):
            raise ShadowModelError("scaler scales must be positive")
    coefficients = logistic.get("coef")
    if not isinstance(coefficients, list) or len(coefficients) != len(FEATURE_NAMES):
        raise ShadowModelError("logistic coef must match ordered feature schema")
    [_finite_number(v, "logistic.coef") for v in coefficients]
    _finite_number(logistic.get("intercept"), "logistic.intercept")
    # A model with no provenance is not accepted as a deployable artifact.
    for key in ("training_cutoff_utc", "dataset_sha256", "research_report_sha256"):
        if not isinstance(artifact.get(key), str) or not artifact[key].strip():
            raise ShadowModelError(f"artifact provenance field {key} is required")
    for key in ("dataset_sha256", "research_report_sha256"):
        digest_value = artifact[key]
        if len(digest_value) != 64 or any(ch not in "0123456789abcdef" for ch in digest_value.lower()):
            raise ShadowModelError(f"artifact provenance field {key} must be a SHA-256 hex digest")
    return artifact, digest


def predict_probability(artifact: dict, features: dict[str, Any]) -> float:
    values = validate_features(features)
    scaler = artifact["standard_scaler"]
    logistic = artifact["logistic_regression"]
    score = _finite_number(logistic["intercept"], "intercept")
    for value, mean, scale, coef in zip(values, scaler["mean"], scaler["scale"], logistic["coef"]):
        score += ((value - float(mean)) / float(scale)) * float(coef)
    if not math.isfinite(score):
        raise ShadowModelError("non-finite model score")
    # Numerically stable sigmoid.
    if score >= 0:
        probability = 1.0 / (1.0 + math.exp(-min(score, 709.0)))
    else:
        exp_score = math.exp(max(score, -709.0))
        probability = exp_score / (1.0 + exp_score)
    return float(probability)


def run_shadow_prediction(payload: dict, artifact_path: str | Path | None = None) -> dict:
    """Return a prediction only; this function cannot open or manage a trade."""
    if not isinstance(payload, dict):
        raise ShadowModelError("payload must be an object")
    symbol = str(payload.get("symbol", "")).upper()
    side = str(payload.get("side", "")).upper()
    if symbol not in SYMBOLS:
        raise ShadowModelError("symbol is outside the frozen Vike universe")
    if side not in ("LONG", "SHORT"):
        raise ShadowModelError("side must be LONG or SHORT")
    if payload.get("timeframe") != "15m":
        raise ShadowModelError("Vike shadow model requires 15m events")
    if payload.get("market_scope") != "BINANCE_USDM_PERPETUAL":
        raise ShadowModelError("Vike shadow model requires Binance USD-M perpetual scope")
    validate_causal_timestamps(
        payload.get("signal_time_ms"), payload.get("feature_available_time_ms"),
        payload.get("htf_1h_close_time_ms"), payload.get("htf_4h_close_time_ms"),
    )
    features = direction_aligned_features(payload.get("raw_features"), side)
    validate_features(features)
    path = artifact_path or os.getenv("ML_SHADOW_MODEL_PATH", "")
    if not path:
        raise ShadowModelError("ML_SHADOW_MODEL_PATH is not configured")
    artifact, digest = load_artifact(path)
    probability = predict_probability(artifact, features)
    return {
        "status": "SHADOW_PREDICTION",
        "model_version": artifact["model_version"],
        "artifact_sha256": digest,
        "symbol": symbol,
        "side": side,
        "timeframe": "15m",
        "signal_time_ms": payload["signal_time_ms"],
        "probability": probability,
        "features": features,
        "diagnostic_only": True,
        "trade_decision_changed": False,
    }
