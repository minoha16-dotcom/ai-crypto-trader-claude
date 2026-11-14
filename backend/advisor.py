"""Local paper-outcome ranking with explicit sample and calibration diagnostics.

The advisor uses only closed, signal-linked paper trades. It is subject to
selection bias because a user chose which signals became trades. Its historical
win-rate estimates are ranking scores, never calibrated probabilities.
"""
import json
import math

from .database import connect

MIN_TRAINING_EXAMPLES = 10
MAX_TRAINING_EXAMPLES = 500
MAX_ROWS_TO_DEDUPLICATE = 5000
NEIGHBOR_COUNT = 25
SIMILARITY_BANDWIDTH = 0.45
MIN_CALIBRATION_EXAMPLES = 30
MIN_CALIBRATION_HOLDOUT = 10


def _bounded(value, low, high, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(high, max(low, parsed))


def _features(item):
    """Normalize scanner metrics so unlike units can be compared locally."""
    score = _bounded(item.get("score"), -3, 3)
    rsi = _bounded(item.get("rsi14"), 0, 100, 50)
    adx = _bounded(item.get("adx14"), 0, 100)
    volume_ratio = _bounded(item.get("volume_ratio"), 0, 100)
    atr_pct = _bounded(item.get("atr_pct"), 0, 100)
    return (
        score / 3,
        (rsi - 50) / 50,
        adx / 50,
        min(1.0, math.log1p(volume_ratio) / math.log(6)),
        min(1.0, math.log1p(atr_pct) / math.log1p(3)),
    )


def _baseline(examples):
    if not examples:
        return 0.5
    wins = sum(1 for _, won in examples if won > 0.5)
    return (wins + 2.0) / (len(examples) + 4.0)


def _similarity_rate(vector, examples, baseline):
    neighbors = []
    for example, won in examples:
        distance_squared = sum(
            (left - right) ** 2 for left, right in zip(vector, example)
        )
        weight = math.exp(
            -distance_squared / (2 * SIMILARITY_BANDWIDTH ** 2)
        )
        neighbors.append((weight, won))
    neighbors.sort(key=lambda pair: pair[0], reverse=True)
    neighbors = neighbors[:NEIGHBOR_COUNT]

    weight_sum = sum(weight for weight, _ in neighbors)
    weighted_wins = sum(weight * won for weight, won in neighbors)
    estimate = (4.0 * baseline + weighted_wins) / (4.0 + weight_sum)
    effective_examples = (
        weight_sum ** 2 / sum(weight ** 2 for weight, _ in neighbors)
        if neighbors and any(weight > 0 for weight, _ in neighbors) else 0.0
    )
    return estimate, effective_examples


def _wilson_interval(rate, sample_size):
    """Approximate 95% Wilson interval; not a calibration guarantee."""
    n = float(sample_size)
    p = float(rate)
    if not math.isfinite(n) or n <= 0 or not math.isfinite(p):
        return None
    p = min(1.0, max(0.0, p))
    z = 1.959963984540054
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    radius = (
        z * math.sqrt((p * (1.0 - p) / n) + z2 / (4.0 * n * n))
        / denominator
    )
    return [round(max(0.0, center - radius), 4), round(min(1.0, center + radius), 4)]


def _chronological_calibration(examples):
    """Rolling-origin holdout; every scored outcome is predicted from older rows."""
    total = len(examples)
    if total < MIN_CALIBRATION_EXAMPLES:
        return {
            "calibrated": False,
            "status": "insufficient_history",
            "method": "rolling_chronological_holdout",
            "total_examples": total,
            "holdout_predictions": 0,
            "out_of_sample_brier_score": None,
            "baseline_brier_score": None,
            "note": "At least 30 unique signal-linked outcomes are required before an out-of-sample diagnostic is reported.",
        }

    first_holdout = max(
        MIN_TRAINING_EXAMPLES,
        int(math.floor(total * 0.7)),
    )
    predictions = []
    baseline_predictions = []
    outcomes = []
    for index in range(first_holdout, total):
        history = examples[:index]
        if len(history) < MIN_TRAINING_EXAMPLES:
            continue
        prior = _baseline(history)
        rate, _ = _similarity_rate(examples[index][0], history, prior)
        predictions.append(rate)
        baseline_predictions.append(prior)
        outcomes.append(examples[index][1])

    count = len(outcomes)
    if count < MIN_CALIBRATION_HOLDOUT:
        return {
            "calibrated": False,
            "status": "insufficient_holdout",
            "method": "rolling_chronological_holdout",
            "total_examples": total,
            "holdout_predictions": count,
            "out_of_sample_brier_score": None,
            "baseline_brier_score": None,
            "note": "The chronological holdout contains too few outcomes for a useful diagnostic.",
        }

    brier = sum((p - y) ** 2 for p, y in zip(predictions, outcomes)) / count
    baseline_brier = sum(
        (p - y) ** 2 for p, y in zip(baseline_predictions, outcomes)
    ) / count
    comparison = (
        "better_than_historical_base_rate"
        if brier < baseline_brier else
        "not_better_than_historical_base_rate"
    )
    return {
        "calibrated": False,
        "status": comparison,
        "method": "rolling_chronological_holdout",
        "total_examples": total,
        "holdout_predictions": count,
        "out_of_sample_brier_score": round(brier, 6),
        "baseline_brier_score": round(baseline_brier, 6),
        "note": (
            "This is a small chronological ranking diagnostic, not probability calibration "
            "or proof of predictive edge."
        ),
    }


def load_training_examples(market_type, timeframe):
    """Load one latest outcome per distinct user-selected scanner signal."""
    with connect() as db:
        rows = db.execute(
            "SELECT t.signal_id, t.closed_at, t.id, t.pnl, s.payload "
            "FROM trades t JOIN signals s ON s.id=t.signal_id "
            "WHERE t.status='CLOSED' AND t.signal_id IS NOT NULL "
            "AND s.market_type=? AND s.timeframe=? "
            "AND s.signal IN ('LONG','SHORT') "
            "ORDER BY t.closed_at DESC, t.id DESC LIMIT ?",
            (market_type, timeframe, MAX_ROWS_TO_DEDUPLICATE),
        ).fetchall()
        all_closed = db.execute(
            "SELECT COUNT(*) AS total FROM trades WHERE status='CLOSED'"
        ).fetchone()["total"]

    seen_signal_ids = set()
    examples = []
    duplicate_rows = 0
    unique_signal_trades = 0
    breakeven_trades = 0
    for row in rows:
        signal_id = row["signal_id"]
        if signal_id in seen_signal_ids:
            duplicate_rows += 1
            continue
        seen_signal_ids.add(signal_id)
        unique_signal_trades += 1
        try:
            item = json.loads(row["payload"])
            if not isinstance(item, dict):
                continue
            pnl = float(row["pnl"] or 0)
            if not math.isfinite(pnl):
                continue
            if abs(pnl) <= 1e-12:
                breakeven_trades += 1
                continue
            examples.append((_features(item), 1.0 if pnl > 0 else 0.0))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(examples) >= MAX_TRAINING_EXAMPLES:
            break

    # SQL returned newest first. Rolling-origin calibration requires oldest first.
    examples.reverse()
    count = len(examples)
    wins = sum(1 for _, won in examples if won > 0.5)
    losses = count - wins
    return examples, {
        "closed_paper_trades": unique_signal_trades,
        "winning_closed_trades": wins,
        "losing_closed_trades": losses,
        "breakeven_closed_trades": breakeven_trades,
        "overall_historical_win_rate": round(_baseline(examples), 4),
        "signal_linked_closed_trades": unique_signal_trades,
        "examples_for_market_timeframe": count,
        "all_closed_paper_trades": int(all_closed or 0),
        "duplicate_signal_trades_excluded": duplicate_rows,
        "outcome_scope": (
            "one latest closed trade per distinct user-selected scanner signal for this "
            "market type and timeframe; exact breakeven PnL is reported separately "
            "and excluded from win/loss training"
        ),
    }


def _technical_score(item):
    """Cold-start ranking; deliberately not presented as a learned probability."""
    return round(
        abs(_bounded(item.get("score"), -3, 3)) * 15
        + min(_bounded(item.get("adx14"), 0, 100), 50) * 0.2
        + min(_bounded(item.get("volume_ratio"), 0, 100), 3) * 5
        + min(_bounded(item.get("atr_pct"), 0, 100), 2) * 2,
        2,
    )


def _learned_win_rate(candidate, examples, baseline):
    return _similarity_rate(_features(candidate), examples, baseline)


def rank_opportunities(scan_results, examples, trade_counts):
    """Rank LONG/SHORT setups with historical scores and explicit limitations."""
    total = len(examples)
    trained = total >= MIN_TRAINING_EXAMPLES
    similarity_ready = trained
    baseline = _baseline(examples)
    calibration = _chronological_calibration(examples)
    candidates = []

    for item in scan_results:
        if item.get("signal") not in ("LONG", "SHORT"):
            continue
        candidate = dict(item)
        if similarity_ready:
            rate, support = _learned_win_rate(candidate, examples, baseline)
            candidate["estimated_historical_win_rate"] = round(rate, 4)
            candidate["historical_win_rate_score"] = round(rate, 4)
            candidate["similar_example_support"] = round(support, 2)
            candidate["historical_outcomes_used"] = total
            candidate["historical_score_interval_95"] = _wilson_interval(
                rate, support + 4.0
            )
            candidate["ranking_method"] = "local_closed_trade_similarity"
            candidate["ranking_confidence"] = (
                "limited_support" if support < 10 else
                "moderate_support" if support < 30 else
                "larger_historical_support"
            )
        else:
            candidate["technical_ranking_score"] = _technical_score(candidate)
            candidate["ranking_method"] = "technical_cold_start"
            candidate["ranking_confidence"] = "technical_only"
            candidate["historical_outcomes_used"] = total
            candidate["probability_calibrated"] = False
        candidate["probability_calibrated"] = False
        candidates.append(candidate)

    if similarity_ready:
        candidates.sort(
            key=lambda item: (
                item["historical_win_rate_score"],
                item["similar_example_support"],
                abs(int(item.get("score") or 0)),
            ),
            reverse=True,
        )
    else:
        candidates.sort(
            key=lambda item: (
                item["technical_ranking_score"],
                abs(int(item.get("score") or 0)),
            ),
            reverse=True,
        )

    return {
        "mode": (
            "local_closed_trade_similarity" if similarity_ready
            else "technical_cold_start"
        ),
        "learning_ready": similarity_ready,
        "history_ready": trained,
        "minimum_examples": MIN_TRAINING_EXAMPLES,
        "training": trade_counts,
        "calibration": calibration,
        "ranked_opportunities": candidates,
        "best_candidate": candidates[0] if candidates else None,
        "note": (
            "النتيجة درجة ترتيب تاريخية من صفقات ورقية اختار المستخدم فتحها، وليست احتمال ربح معايراً. "
            "العينة لا تشمل الإشارات التي لم تُفتح، لذلك قد تتأثر بانحياز الاختيار."
            if similarity_ready else
            "لا توجد عينة كافية من الصفقات الورقية المرتبطة بإشارات ممسوحة؛ الترتيب تقني أولي ولا يعرض تقديراً تاريخياً."
        ),
        "calibration_note": calibration["note"],
        "execution": "اقتراح بحثي فقط؛ لا تُفتح أي صفقة تلقائياً.",
    }


def analyze_closed_trade_history(scan_results, market_type, timeframe):
    examples, counts = load_training_examples(market_type, timeframe)
    return rank_opportunities(scan_results, examples, counts)
