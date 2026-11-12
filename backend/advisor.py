"""Local, paper-trade outcome based ranking for scanner opportunities.

This intentionally uses only public scanner features and closed paper trades.
It does not call an external model, access private exchange APIs, or execute trades.
"""
import json
import math

from .database import connect

MIN_TRAINING_EXAMPLES = 10
MAX_TRAINING_EXAMPLES = 500
NEIGHBOR_COUNT = 25
SIMILARITY_BANDWIDTH = 0.45


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


def load_training_examples(market_type, timeframe):
    """Read only closed, signal-linked paper trades for this market/timeframe."""
    with connect() as db:
        rows = db.execute(
            "SELECT t.pnl, s.payload FROM trades t "
            "JOIN signals s ON s.id=t.signal_id "
            "WHERE t.status='CLOSED' AND t.signal_id IS NOT NULL "
            "AND s.market_type=? AND s.timeframe=? "
            "ORDER BY t.closed_at DESC, t.id DESC LIMIT ?",
            (market_type, timeframe, MAX_TRAINING_EXAMPLES),
        ).fetchall()
        counts = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN signal_id IS NOT NULL THEN 1 ELSE 0 END) AS linked "
            "FROM trades WHERE status='CLOSED'"
        ).fetchone()

    examples = []
    for row in rows:
        try:
            item = json.loads(row["payload"])
            if not isinstance(item, dict):
                continue
            pnl = float(row["pnl"] or 0)
            if not math.isfinite(pnl):
                continue
            examples.append((_features(item), 1.0 if pnl > 0 else 0.0))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    closed_count = int(counts["total"] or 0)
    wins = int(counts["wins"] or 0)
    return examples, {
        "closed_paper_trades": closed_count,
        "winning_closed_trades": wins,
        "overall_historical_win_rate": round((wins + 2) / (closed_count + 4), 4),
        "signal_linked_closed_trades": int(counts["linked"] or 0),
        "examples_for_market_timeframe": len(examples),
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
    vector = _features(candidate)
    neighbors = []
    for example, won in examples:
        distance_squared = sum((left - right) ** 2 for left, right in zip(vector, example))
        weight = math.exp(-distance_squared / (2 * SIMILARITY_BANDWIDTH ** 2))
        neighbors.append((weight, won))
    neighbors.sort(key=lambda pair: pair[0], reverse=True)
    neighbors = neighbors[:NEIGHBOR_COUNT]

    # A small prior from the complete closed-trade record avoids pretending that
    # every strategy starts at 50%; nearby signal-linked outcomes then refine it.
    weight_sum = sum(weight for weight, _ in neighbors)
    weighted_wins = sum(weight * won for weight, won in neighbors)
    estimate = (4.0 * baseline + weighted_wins) / (4.0 + weight_sum)
    effective_examples = (
        weight_sum ** 2 / sum(weight ** 2 for weight, _ in neighbors)
        if neighbors and any(weight > 0 for weight, _ in neighbors) else 0.0
    )
    return estimate, effective_examples


def rank_opportunities(scan_results, examples, trade_counts):
    """Rank tradeable LONG/SHORT scan results using outcomes when available."""
    total_closed = trade_counts["closed_paper_trades"]
    trained = total_closed >= MIN_TRAINING_EXAMPLES
    similarity_ready = len(examples) >= MIN_TRAINING_EXAMPLES
    baseline = trade_counts["overall_historical_win_rate"]
    candidates = []
    for item in scan_results:
        if item.get("signal") not in ("LONG", "SHORT"):
            continue
        candidate = dict(item)
        if similarity_ready:
            rate, support = _learned_win_rate(candidate, examples, baseline)
            candidate["estimated_historical_win_rate"] = round(rate, 4)
            candidate["similar_example_support"] = round(support, 2)
            candidate["ranking_method"] = "local_closed_trade_similarity"
        elif trained:
            candidate["estimated_historical_win_rate"] = baseline
            candidate["similar_example_support"] = 0.0
            candidate["ranking_method"] = "closed_trade_history_baseline"
        else:
            candidate["technical_ranking_score"] = _technical_score(candidate)
            candidate["ranking_method"] = "technical_cold_start"
        candidates.append(candidate)

    if similarity_ready:
        candidates.sort(
            key=lambda item: (
                item["estimated_historical_win_rate"],
                item["similar_example_support"],
                abs(int(item.get("score") or 0)),
            ),
            reverse=True,
        )
    elif trained:
        candidates.sort(
            key=lambda item: (
                item["estimated_historical_win_rate"],
                abs(int(item.get("score") or 0)),
            ),
            reverse=True,
        )
    else:
        candidates.sort(
            key=lambda item: (item["technical_ranking_score"], abs(int(item.get("score") or 0))),
            reverse=True,
        )

    return {
        "mode": (
            "local_closed_trade_similarity" if similarity_ready else
            "closed_trade_history_baseline" if trained else
            "technical_cold_start"
        ),
        "learning_ready": similarity_ready,
        "history_ready": trained,
        "minimum_examples": MIN_TRAINING_EXAMPLES,
        "training": trade_counts,
        "ranked_opportunities": candidates,
        "best_candidate": candidates[0] if candidates else None,
        "note": (
            "الترتيب يستند إلى تشابه الإشارات مع صفقات ورقية مغلقة؛ النسبة تاريخية وليست توقعاً مضموناً."
            if similarity_ready else
            "يوجد سجل صفقات مغلقة، لكن لا تكفي الإشارات المرتبطة لتخصيص الترتيب؛ تُستخدم نسبة السجل العام مع ترتيب تقني فاصل."
            if trained else
            "لا يوجد سجل مغلق كافٍ بعد؛ الترتيب تقني أولي ولا يعرض احتمال ربح متعلّماً."
        ),
        "execution": "اقتراح فقط؛ لا تُفتح أي صفقة تلقائياً.",
    }


def analyze_closed_trade_history(scan_results, market_type, timeframe):
    examples, counts = load_training_examples(market_type, timeframe)
    return rank_opportunities(scan_results, examples, counts)

