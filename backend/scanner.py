import json
import asyncio
from .exchanges import registry, ExchangeRateLimitCooldown
from .indicators import analyze
from .database import connect, archive_expired_signals
from .config import settings


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


async def scan(exchange,symbols=None,market_type='spot',timeframe=None):
    symbols=symbols or settings.symbols; timeframe=timeframe or settings.scan_timeframe; results=[]
    for symbol in symbols:
        try:
            resolved_symbol = await registry.resolve_symbol(exchange, symbol, market_type)
            candles=await registry.ohlcv(exchange,resolved_symbol,timeframe,120,market_type); metrics=analyze(candles[:-1])
            item={'exchange':exchange,'symbol':resolved_symbol,'market_type':market_type,'timeframe':timeframe,**metrics}
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
            results.append({'exchange':exchange,'symbol':symbol,'error':str(exc)})
    return results

