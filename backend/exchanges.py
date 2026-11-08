"""Public market-data adapters only. No exchange credentials or private trading methods."""
import asyncio
import ccxt.async_support as ccxt
EXCHANGE_IDS = ('binance', 'bybit', 'okx')
class ExchangeRegistry:
    def __init__(self): self._clients = {}
    async def get(self, exchange_id, market_type='spot'):
        if exchange_id not in EXCHANGE_IDS: raise ValueError('Unsupported exchange: ' + exchange_id)
        key = (exchange_id, market_type)
        if key not in self._clients:
            klass = getattr(ccxt, exchange_id)
            options = {'defaultType': 'swap' if market_type == 'futures' else 'spot'}
            self._clients[key] = klass({'enableRateLimit': True, 'options': options})
        return self._clients[key]
    async def ticker(self, exchange_id, symbol, market_type='spot'):
        return await (await self.get(exchange_id, market_type)).fetch_ticker(symbol)
    async def ohlcv(self, exchange_id, symbol, timeframe='1h', limit=150, market_type='spot'):
        return await (await self.get(exchange_id, market_type)).fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    async def close(self):
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)
        self._clients.clear()
registry = ExchangeRegistry()
