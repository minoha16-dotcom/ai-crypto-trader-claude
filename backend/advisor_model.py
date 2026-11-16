"""Small, deterministic R-multiple ranking model for Advisor research.

This model is fitted only on closed outcomes produced by the shared backtest.
It deliberately uses five normalized features and a Ridge L2 penalty.
"""
import math

MODEL_VERSION = "advisor-ridge-r-1.0.0"
FEATURE_SCHEMA_VERSION = "advisor-r-features-1"
FEATURE_NAMES = (
    "directional_composite_trend",
    "directional_rsi",
    "adx_strength",
    "log_volume_ratio",
    "log_atr_percent",
)
DEFAULT_ALPHA = 2.0
ELIGIBLE_EXIT_REASONS = {
    "STOP", "STOP_GAP", "TARGET", "TARGET_GAP", "TIME_EXIT",
}


def _finite(value, name):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def features_from_signal(sample):
    """Build the five-feature vector from information known at signal close.

    log(close / EMA50) is the composite of log(close / EMA20) and
    log(EMA20 / EMA50), so the two correlated trend votes are represented once.
    """
    metrics = sample.get("signal_features")
    if not isinstance(metrics, dict):
        raise ValueError("signal_features must be an object")

    side = str(sample.get("side", "")).upper()
    direction = 1.0 if side == "LONG" else -1.0 if side == "SHORT" else 0.0
    if direction == 0:
        raise ValueError("signal side must be LONG or SHORT")

    close = _finite(metrics.get("price"), "price")
    ema50 = _finite(metrics.get("ema50"), "ema50")
    rsi = _finite(metrics.get("rsi14"), "rsi14")
    adx = _finite(metrics.get("adx14"), "adx14")
    volume_ratio = _finite(metrics.get("volume_ratio"), "volume_ratio")
    atr_pct = _finite(metrics.get("atr_pct"), "atr_pct")
    if min(close, ema50) <= 0 or volume_ratio < 0 or atr_pct < 0:
        raise ValueError("price, EMA50, volume ratio and ATR percent must be valid")

    trend = direction * math.tanh(5.0 * math.log(close / ema50))
    directional_rsi = direction * max(-1.0, min(1.0, (rsi - 50.0) / 50.0))
    adx_strength = max(0.0, min(2.0, adx / 50.0))
    log_volume_ratio = max(0.0, min(2.0, math.log1p(volume_ratio) / math.log(6.0)))
    log_atr_percent = max(0.0, min(2.0, math.log1p(atr_pct) / math.log(4.0)))
    vector = (
        trend,
        directional_rsi,
        adx_strength,
        log_volume_ratio,
        log_atr_percent,
    )
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("derived signal features must be finite")
    return vector


def _solve_linear_system(matrix, vector):
    """Gaussian elimination with partial pivoting; matrices are only 6x6."""
    size = len(vector)
    augmented = [
        [float(value) for value in matrix[row]] + [float(vector[row])]
        for row in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("Ridge system is singular or numerically unstable")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][size] for row in range(size)]


def fit_ridge(training_rows, alpha=DEFAULT_ALPHA):
    """Fit a Ridge regression model to net R labels from run_backtest."""
    penalty = _finite(alpha, "alpha")
    if penalty <= 0:
        raise ValueError("alpha must be greater than zero")
    if not isinstance(training_rows, (list, tuple)) or not training_rows:
        raise ValueError("at least one training row is required")

    prepared = []
    for index, row in enumerate(training_rows):
        if not isinstance(row, dict):
            raise ValueError(f"training row {index} must be an object")
        exit_reason = str(row.get("exit_reason", "")).upper()
        if exit_reason not in ELIGIBLE_EXIT_REASONS:
            continue
        target = _finite(row.get("r_multiple"), f"training row {index} r_multiple")
        vector = row.get("features")
        if vector is None:
            vector = features_from_signal(row)
        if not isinstance(vector, (list, tuple)) or len(vector) != len(FEATURE_NAMES):
            raise ValueError(f"training row {index} must contain five features")
        clean = [_finite(value, f"training row {index} feature") for value in vector]
        prepared.append(([1.0] + clean, target))

    if not prepared:
        raise ValueError("no eligible training rows with valid features and R labels")
    width = len(FEATURE_NAMES) + 1
    gram = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for vector, target in prepared:
        for left in range(width):
            rhs[left] += vector[left] * target
            for right in range(width):
                gram[left][right] += vector[left] * vector[right]
    # Do not regularize the intercept.
    for index in range(1, width):
        gram[index][index] += penalty

    coefficients = _solve_linear_system(gram, rhs)
    model = {
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "algorithm": "ridge_regression_l2",
        "feature_names": list(FEATURE_NAMES),
        "alpha": penalty,
        "intercept": coefficients[0],
        "weights": coefficients[1:],
        "training_samples": len(prepared),
        "target": "net_r_multiple",
        "label_source": "shared_run_backtest",
    }
    return model


def fit_ridge_on_targets(
    training_rows,
    *,
    target_key,
    target_name,
    model_version,
    alpha=DEFAULT_ALPHA,
):
    """Fit a separate Ridge target, such as public expert pnlRatio (never net R)."""
    penalty = _finite(alpha, "alpha")
    if penalty <= 0:
        raise ValueError("alpha must be greater than zero")
    if not isinstance(training_rows, (list, tuple)) or not training_rows:
        raise ValueError("at least one training row is required")
    prepared = []
    for index, row in enumerate(training_rows):
        if not isinstance(row, dict):
            raise ValueError(f"training row {index} must be an object")
        target = _finite(row.get(target_key), f"training row {index} {target_key}")
        vector = row.get("features")
        if not isinstance(vector, (list, tuple)) or len(vector) != len(FEATURE_NAMES):
            raise ValueError(f"training row {index} must contain five features")
        clean = [_finite(value, f"training row {index} feature") for value in vector]
        prepared.append(([1.0] + clean, target))
    width = len(FEATURE_NAMES) + 1
    gram = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for vector, target in prepared:
        for left in range(width):
            rhs[left] += vector[left] * target
            for right in range(width):
                gram[left][right] += vector[left] * vector[right]
    for index in range(1, width):
        gram[index][index] += penalty
    coefficients = _solve_linear_system(gram, rhs)
    return {
        "model_version": model_version,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "algorithm": "ridge_regression_l2",
        "feature_names": list(FEATURE_NAMES),
        "alpha": penalty,
        "intercept": coefficients[0],
        "weights": coefficients[1:],
        "training_samples": len(prepared),
        "target": target_name,
        "label_source": "public_okx_copy_trading_history",
    }


def predict_target(model, sample):
    """Predict the model's declared target; the caller must preserve its unit."""
    vector = sample.get("features")
    if vector is None:
        vector = features_from_signal(sample)
    if not isinstance(vector, (list, tuple)) or len(vector) != len(FEATURE_NAMES):
        raise ValueError("prediction row must contain five features")
    clean = [_finite(value, "prediction feature") for value in vector]
    weights = model.get("weights")
    if not isinstance(weights, (list, tuple)) or len(weights) != len(FEATURE_NAMES):
        raise ValueError("model weights do not match the feature schema")
    intercept = _finite(model.get("intercept"), "model intercept")
    prediction = intercept + sum(
        _finite(weight, "model weight") * value
        for weight, value in zip(weights, clean)
    )
    if not math.isfinite(prediction):
        raise ValueError("model returned a non-finite prediction")
    return prediction


def predict_r_multiple(model, sample):
    """Predict true strategy net R only; reject non-R auxiliary models."""
    if model.get("target") != "net_r_multiple":
        raise ValueError("model target is not net_r_multiple")
    return predict_target(model, sample)
