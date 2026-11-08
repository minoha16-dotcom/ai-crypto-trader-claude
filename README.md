# AI Crypto Trader V2

An AI-assisted cryptocurrency research and paper-trading toolkit built around Claude Skills and a local Python/FastAPI dashboard.

> **Safety first:** this project is for market research and paper trading. Exchange adapters read public market data only. There is no live order execution, no private exchange API access, and no withdrawal functionality. Nothing here is financial advice.

## The Trader skill

The Trader skill helps structure a repeatable crypto-research workflow:

- Review market conditions and identify the current market regime.
- Analyze price action with EMA 20/50, RSI 14, and ATR 14.
- Compare Spot and Futures market context.
- Consider derivatives, on-chain data, and crypto news where relevant.
- Evaluate position sizing and portfolio exposure before recording a simulated trade.
- Keep a trade journal and review strategies through backtesting workflows.

Use the skills as research assistance, not as an autonomous trading signal or a guarantee of returns.

## Install

### Requirements

- Python 3.11+
- Git
- Claude, if you want to use the repository's Claude plugin and Skills

### Backend installation

Clone the repository and enter its directory:

```bash
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
cd ai-crypto-trader-claude
```

Create and activate a virtual environment:

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux
source .venv/bin/activate
```

Install dependencies and configure the environment:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and review the risk limits before starting the application.

### Claude plugin

Add this repository as a marketplace/plugin source in Claude's supported plugin settings. Enable the Skills you want to use. Exact installation steps may vary with the Claude client and its current plugin interface.

## The eleven

The repository includes a collection of focused Skills for the trading workflow:

1. **crypto-market-analysis** — broad market structure and asset context.
2. **technical-analysis** — indicators, trend, momentum, and price structure.
3. **onchain-analysis** — blockchain activity and on-chain metrics.
4. **crypto-news** — news context and event awareness.
5. **risk-management** — risk budgets, exposure, and trade invalidation.
6. **spot-trading** — Spot-market research workflows.
7. **futures-trading** — Futures-market research workflows.
8. **market-regime** — trending, ranging, and changing market conditions.
9. **derivatives-analysis** — derivatives metrics and positioning context.
10. **portfolio-allocation** — allocation and portfolio exposure considerations.
11. **trade-journal** — recording and reviewing trade decisions.
12. **strategy-backtesting** — structured historical strategy evaluation.

The heading “The eleven” is retained as requested; the current repository contains **12 Skills**.

## How to use it

### Start the dashboard

```bash
uvicorn backend.main:app --reload
```

Then open:

- Dashboard: http://127.0.0.1:8000
- API documentation: http://127.0.0.1:8000/docs

### Suggested workflow

1. Select an exchange and choose Spot or Futures market-data mode.
2. Run a scan for supported symbols and a timeframe.
3. Review the market data, indicator values, and generated research signals.
4. Check the risk limits and decide whether a simulated position is appropriate.
5. Open a paper trade from the dashboard/API.
6. Close the paper trade by supplying a simulated exit price.
7. Review the journal and performance history.

Example scan request:

```bash
curl -X POST "http://127.0.0.1:8000/api/scan?exchange=binance&market_type=spot&symbols=BTC/USDT,ETH/USDT&timeframe=1h"
```

## The tools

### Market data and analysis

- **CCXT** connects to public market-data endpoints for Binance, Bybit, and OKX.
- **FastAPI** serves the backend and REST API.
- **Pandas / NumPy** support data handling and indicator calculations.
- **EMA 20/50, RSI 14, ATR 14** provide basic trend, momentum, and volatility context.

### Risk and persistence

- Central risk checks include per-trade risk, aggregate open risk, daily realized-loss limits, and a maximum position-notional cap.
- **SQLite** stores paper trades and research signals locally.
- The browser dashboard displays scans, simulated positions, account summary, performance, and journal history.

### Useful API routes

- `GET /api/health`
- `GET /api/exchanges`
- `GET /api/markets/{exchange}/{symbol}?market_type=spot|futures`
- `POST /api/scan`
- `GET /api/signals`
- `GET /api/account`
- `GET /api/risk`
- `GET /api/journal`
- `POST /api/paper/open`
- `POST /api/paper/{trade_id}/close`

## Fine print

- **Paper trading only:** the project does not place real orders or connect to private account endpoints.
- **No credentials required:** market adapters use public data. Do not add exchange secrets to the repository.
- **Futures limitations:** Futures mode selects swap markets through CCXT; symbols, contract availability, and venue behavior vary.
- **Simulation limitations:** paper exits are entered manually. The starter does not fully simulate order-book slippage, funding payments, liquidation, or automatic stop-loss/take-profit execution.
- **Signals are heuristic:** indicator-based scans are not validated predictive models and can be wrong.
- **Risk controls are not a guarantee:** configured limits do not eliminate market, software, or execution risk.
- **Local storage:** SQLite is intended for local use. Set up backups and persistent storage before any deployment.
- **Testing:** run the test suite in your environment before relying on changes. No claim is made here that tests have passed in your environment.
- **Not financial advice:** make independent decisions and comply with applicable laws and exchange terms.

## Files

```text
.
├── backend/
│   ├── main.py
│   └── dashboard.html
├── tests/
│   └── test_core.py
├── .env.example
├── requirements.txt
├── README.md
├── LICENSE
└── skills/
    ├── crypto-market-analysis/
    ├── technical-analysis/
    ├── onchain-analysis/
    ├── crypto-news/
    ├── risk-management/
    ├── spot-trading/
    ├── futures-trading/
    ├── market-regime/
    ├── derivatives-analysis/
    ├── portfolio-allocation/
    ├── trade-journal/
    └── strategy-backtesting/
```

Run the test suite with:

```bash
pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
