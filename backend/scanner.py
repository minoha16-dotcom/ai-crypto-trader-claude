import json
import asyncio

from .exchanges import registry, ExchangeRateLimitCooldown
from .indicators import analyze, market_context
from .database import connect, archive_expired_signals
from .config import settings
from .data_validation import validate_scan_candles, MIN_SCAN_CANDLES
from .strategy_approval import approval_for_group


def _persist_signal(exchange, symbol, market_type, timeframe, metrics, item):
    with connect() as db:
        cursor = db.execute(
            'INSERT INTO signals(exchange,symbol,market_type,timeframe,signal,score,price,rsi,trend,payload) '
            'VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id',
            (exchange,symbol,market_type,timeframe,metrics['signal'],metrics['score'],metrics['price'],metrics['rsi14'],metrics['trend'],json.dumps(item)),
        )
        signal_id = cursor.fetchone()["id"]
    archive_expired_signals()
    return signal_id


def apply_higher_timeframe_confirmation(metrics, higher_context):
    """Require aligned trending direction; neutral or ranging higher TF blocks."""
    side = metrics.get("signal")
    required_direction = "bullish" if side == "LONG" else "bearish" if side == "SHORT" else None
    confirmed = (
        required_direction is not None
        and higher_context.get("regime") == "trending"
        and higher_context.get("direction") == required_direction
    )
    if not confirmed:
        metrics["signal"] = "WAIT"
        metrics.setdefault("filter_reasons", []).append("MTF_DIRECTION_NOT_CONFIRMED")
    return confirmed


def _data_rejection(exchange, symbol, market_type, timeframe, error):
    message = str(error)
    reason_code = message.split(":", 1)[0] if ":" in message else "DATA_VALIDATION_FAILED"
    return {
        "exchange": exchange,
        "symbol": symbol,
        "market_type": market_type,
        "timeframe": timeframe,
        "status": "DATA_REJECTED",
        "signal": "WAIT",
        "reason_code": reason_code,
        "error": message,
    }


async def scan(exchange, symbols=None, market_type="spot", timeframe=None):
    symbols = symbols or settings.symbols
    timeframe = timeframe or settings.scan_timeframe
    results = []
    for symbol in symbols:
        try:
            resolved_symbol = await registry.resolve_symbol(exchange, symbol, market_type)
            # Fetch one extra row so the validator can remove a still-forming
            # candle while retaining 500 fully closed candles for indicators.
            raw_candles = await registry.ohlcv(
                exchange, resolved_symbol, timeframe, MIN_SCAN_CANDLES + 1, market_type
            )
            try:
                candles = validate_scan_candles(
                    raw_candles, timeframe, min_candles=MIN_SCAN_CANDLES
                )
            except ValueError as exc:
                results.append(_data_rejection(
                    exchange, resolved_symbol, market_type, timeframe, exc
                ))
                continue

            metrics = analyze(candles)
            item_status = "CANDIDATE"
            if timeframe == "15m" and metrics["signal"] in ("LONG", "SHORT"):
                higher_timeframe = settings.signal_higher_timeframe
                raw_higher = await registry.ohlcv(
                    exchange, resolved_symbol, higher_timeframe,
                    MIN_SCAN_CANDLES + 1, market_type,
                )
                try:
                    higher_candles = validate_scan_candles(
                        raw_higher, higher_timeframe, min_candles=MIN_SCAN_CANDLES
                    )
                    higher_context = market_context(higher_candles)
                except ValueError as exc:
                    rejected = _data_rejection(
                        exchange, resolved_symbol, market_type, higher_timeframe, exc
                    )
                    rejected["status"] = "MTF_DATA_REJECTED"
                    rejected["signal_timeframe"] = timeframe
                    results.append(rejected)
                    continue
                metrics["higher_timeframe"] = higher_timeframe
                metrics["higher_timeframe_context"] = higher_context
                if not apply_higher_timeframe_confirmation(metrics, higher_context):
                    item_status = "MTF_REJECTED"

            approved, approval_reason, artifact = approval_for_group(
                exchange, resolved_symbol, market_type, timeframe
            )
            if not approved:
                metrics["signal"] = "WAIT"
                metrics.setdefault("filter_reasons", []).append(approval_reason)
                item_status = "STRATEGY_NOT_APPROVED"

            item = {
                "exchange": exchange,
                "symbol": resolved_symbol,
                "market_type": market_type,
                "timeframe": timeframe,
                "data_status": "VALIDATED",
                "status": item_status,
                "strategy_version": artifact.get("strategy_version") if artifact else None,
                "strategy_approval_reason": approval_reason,
                "closed_candles": len(candles),
                **metrics,
            }
            signal_id = await asyncio.to_thread(
                _persist_signal, exchange, resolved_symbol, market_type, timeframe,
                metrics, item,
            )
            item["signal_id"] = signal_id
            results.append(item)
        except ExchangeRateLimitCooldown:
            # Preserve HTTP 429 semantics; do not disguise cooldowns as a symbol error.
            raise
        except Exception as exc:
            results.append({
                "exchange": exchange,
                "symbol": symbol,
                "status": "SCAN_ERROR",
                "error": str(exc),
            })
    return results
