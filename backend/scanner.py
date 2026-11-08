import json
from .exchanges import registry
from .indicators import analyze
from .database import connect
from .config import settings
async def scan(exchange,symbols=None,market_type='spot',timeframe=None):
    symbols=symbols or settings.symbols; timeframe=timeframe or settings.scan_timeframe; results=[]
    for symbol in symbols:
        try:
            candles=await registry.ohlcv(exchange,symbol,timeframe,120,market_type); metrics=analyze(candles)
            item={'exchange':exchange,'symbol':symbol,'market_type':market_type,'timeframe':timeframe,**metrics}; results.append(item)
            with connect() as db:
                db.execute('INSERT INTO signals(exchange,symbol,market_type,timeframe,signal,score,price,rsi,trend,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',(exchange,symbol,market_type,timeframe,metrics['signal'],metrics['score'],metrics['price'],metrics['rsi14'],metrics['trend'],json.dumps(item)))
        except Exception as exc: results.append({'exchange':exchange,'symbol':symbol,'error':str(exc)})
    return results
