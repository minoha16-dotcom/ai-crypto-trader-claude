import json
import asyncio

from .exchanges import registry, ExchangeRateLimitCooldown
from .indicators import analyze
from .database import connect, archive_expired_signals
from .config import settings
from .data_validation import validate_scan_candles, MIN_SCAN_CANDLES


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
            item = {
                "exchange": exchange,
                "symbol": resolved_symbol,
                "market_type": market_type,
                "timeframe": timeframe,
                "data_status": "VALIDATED",
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
