# AI Crypto Trader V2

A Claude plugin plus a Python/FastAPI research and paper-trading dashboard for cryptocurrency markets.

> **Safety by design:** exchange adapters use public market data only. Paper trades are simulated and stored locally. There are no private exchange endpoints, live order methods, withdrawal permissions, or promises of profitability. Not financial advice.

## V2 features
- Spot and Futures market-data mode selection.
- Multi-exchange adapters for Binance, Bybit, and OKX via CCXT public endpoints.
- Opportunity scanner using EMA 20/50, RSI 14, and ATR 14.
- Central risk engine: per-trade risk budget, max aggregate open risk, daily realized-loss guard, and position-notional cap.
- Paper-only trade opening and closing with local SQLite persistence.
- Interactive browser dashboard for scans, simulated positions, account summary, performance chart, and journal.
- Persistent signal and trade history.
- 12 Claude Skills: market analysis, technical analysis, on-chain analysis, news, risk, Spot, Futures, market regime, derivatives, portfolio allocation, trade journal, and backtesting.

## Run locally
1. Use Python 3.11 or newer.
2. Create an environment and install dependencies:
   ```bash
   python -m venv .venv
   # Windows: .venv\\Scripts\\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and adjust risk settings if needed.
4. Start the server:
   ```bash
   uvicorn backend.main:app --reload
   ```
5. Open http://127.0.0.1:8000 for the dashboard and http://127.0.0.1:8000/docs for API docs.

## API overview
- `GET /api/health`, `GET /api/exchanges`
- `GET /api/markets/{exchange}/{symbol}?market_type=spot|futures`
- `POST /api/scan?exchange=binance&market_type=spot&symbols=BTC/USDT,ETH/USDT&timeframe=1h`
- `GET /api/signals`, `GET /api/account`, `GET /api/risk`, `GET /api/journal`
- `POST /api/paper/open`, `POST /api/paper/{trade_id}/close`

## Run tests
```bash
pytest -q
```

## Important implementation limits
- Exchange integrations are public market data adapters; no API keys are required.
- Futures mode selects the venue's swap market through CCXT. Contract availability and symbol formats vary by exchange.
- Scanner signals are simple indicator heuristics, not validated alpha or financial advice.
- Paper fills use manual simulated exit prices; the starter does not yet simulate order-book slippage, funding payments, liquidation, or automatic stop/target execution.
- SQLite is the current local journal store. Configure a persistent volume before deploying.
- The dashboard is served by the FastAPI backend; it is not a hosted service.

## Claude plugin
Add marketplace `minoha16-dotcom/ai-crypto-trader-claude` in Claude's plugin settings. The plugin's 12 Skills and commands provide research workflows; the separate backend dashboard is started locally using the steps above.

## Security
Never commit exchange secrets. The current code does not need credentials. If private exchange connectivity is introduced later, use least-privilege read-only keys first and keep withdrawals disabled.

## License
MIT.
