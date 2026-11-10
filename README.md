# AI Crypto Trader V2

An AI-assisted cryptocurrency research and paper-trading toolkit built around Claude Skills, a Claude Code plugin, and an optional local Python/FastAPI dashboard.

> **Safety first:** this project is for market research and paper trading. Exchange adapters read public market data only. There is no live order execution, no private exchange API access, and no withdrawal functionality. Nothing here is financial advice.

## Install the Claude Skills

### Quick install in Claude

Paste this into Claude:

```text
https://github.com/minoha16-dotcom/ai-crypto-trader-claude

Install the AI Crypto Trader skills from this repository. Explain how to use the 12 skills and keep all trading activity paper-only. Do not request exchange API keys or place live orders.
```

### Install as a Claude Code plugin

In Claude Code, run:

```text
/plugin marketplace add minoha16-dotcom/ai-crypto-trader-claude
/plugin install ai-crypto-trader@ai-crypto-trader-marketplace
```

The plugin provides namespaced skills, for example:

- `/ai-crypto-trader:crypto-market-analysis`
- `/ai-crypto-trader:technical-analysis`
- `/ai-crypto-trader:risk-management`

It also includes commands such as:

```text
/ai-crypto-trader:analyze BTC/USDT Spot 1h
/ai-crypto-trader:risk-check BTC/USDT LONG entry=65000 stop=64000
```

### Install as plain Claude Code skills

If you want un-namespaced skill names instead of plugin-prefixed names, clone the repository and copy the skill folders.

macOS / Linux:

```bash
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
mkdir -p ~/.claude/skills
cp -R ai-crypto-trader-claude/plugins/ai-crypto-trader/skills/* ~/.claude/skills/
```

Project-local installation (available only in that repository):

```bash
mkdir -p .claude/skills
cp -R ai-crypto-trader-claude/plugins/ai-crypto-trader/skills/* .claude/skills/
```

Windows PowerShell:

```powershell
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
New-Item -ItemType Directory -Force "$HOME\.claude\skills"
Copy-Item "ai-crypto-trader-claude\plugins\ai-crypto-trader\skills\*" "$HOME\.claude\skills" -Recurse -Force
```

The plain skill folders are Markdown instructions and do not require Python packages. You can also open any individual `SKILL.md` and paste its contents into a chat to use that skill as a one-off mode. In a normal chat, this does not provide the local dashboard, exchange-data backend, or persistent paper-trading database.

## The Trader skill

The Trader skill collection helps structure a repeatable crypto-research workflow:

- Review market conditions and identify the current market regime.
- Analyze price action with EMA 20/50, RSI 14, and ATR 14.
- Compare Spot and Futures market context.
- Consider derivatives, on-chain data, and crypto news where relevant.
- Evaluate position sizing and portfolio exposure before recording a simulated trade.
- Keep a trade journal and review strategies through backtesting workflows.

Use the skills as research assistance, not as an autonomous trading signal or a guarantee of returns.

## The twelve skills

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

## Optional Python dashboard and API

The Claude Skills/plugin can be installed independently. The local dashboard and API are an optional separate component.

### Requirements

- Python 3.11+
- Git
- pip
- Claude Code only if you want the plugin or local Skills integration

### Install the backend

```bash
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
cd ai-crypto-trader-claude
python -m venv .venv
```

Activate the environment:

```bash
# Windows
.venv\\Scripts\\activate

# macOS / Linux
source .venv/bin/activate
```

Install the backend dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and review the risk limits before starting the application.

### Run the dashboard

```bash
uvicorn backend.main:app --reload
```

Open:

- Dashboard: http://127.0.0.1:8000
- API documentation: http://127.0.0.1:8000/docs

## How to use it

1. Install the Claude Code plugin or copy the Skills into your Claude Skills directory.
2. Invoke a skill such as `/ai-crypto-trader:crypto-market-analysis`, or use the `/analyze` command.
3. Specify the asset, exchange, timeframe, and Spot or Futures context.
4. Start the optional backend if you need the dashboard, API, or local paper-trade journal.
5. Review market data, indicator values, and research signals.
6. Check risk limits before recording a simulated position.
7. Close paper trades with a simulated exit price and review the journal.

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
- **Testing:** run `pytest -q` in the repository. GitHub Actions is configured to run the test suite on pushes and pull requests; check the Actions tab for the actual result.
- **Not financial advice:** make independent decisions and comply with applicable laws and exchange terms.

## Files

```text
.
├── .claude-plugin/
│   └── marketplace.json
├── .github/workflows/
│   └── tests.yml
├── plugins/ai-crypto-trader/
│   ├── .claude-plugin/plugin.json
│   ├── commands/
│   └── skills/
├── backend/
├── tests/
├── .env.example
├── requirements.txt
├── README.md
└── LICENSE
```

## License

MIT. See [LICENSE](LICENSE).


## Render deployment (persistent paper dashboard)

A Render Blueprint is provided in `render.yaml`. In Render choose **New → Blueprint**, connect this repository, and select the `deploy/render-paper-dashboard` branch.

- The service uses the paid `0.5c-512mb` compute plan and a persistent 1 GB disk mounted at `/var/data`; SQLite is stored at `/var/data/trader.db`.
- Render generates `DASHBOARD_PASSWORD`. The initial dashboard username is `admin`; retrieve the generated password from the Render service Environment page and keep it private.
- HTTP Basic Authentication protects the dashboard and every API route except `/api/health`, which is public for health checks. API docs are disabled.
- Scanning is manual: use the dashboard Scan button. No scheduler or unattended trading process is enabled.
- The app remains paper-only and uses public market data; it does not need exchange API keys or submit live orders.

After deployment, check `/api/health` and sign in at the service URL. Rotate the generated password in Render if it is exposed. Render paid compute plans and persistent disks are paid features; review current pricing before applying the Blueprint.


## Render + Supabase deployment (PostgreSQL)

For a Render web service that uses Supabase for persistent paper-trading data:

1. Create a Supabase project and select **Connect → Direct → Session pooler → URI**. Use the Session Pooler for Render's IPv4 network path.
2. In Render, create a Python Web Service from this repository and select the `deploy/render-supabase-postgres` branch.
3. Set the build command to `pip install -r requirements.txt`.
4. Set the start command to `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`.
5. Add these Render environment variables:
   - `DATABASE_URL`: the complete Supabase Session Pooler URI, with the database password substituted for `[YOUR-PASSWORD]`. Keep it secret; do not commit it.
   - `DASHBOARD_USERNAME`: a private dashboard username.
   - `DASHBOARD_PASSWORD`: a long, unique password.
6. Do not add Supabase `service_role`, `anon`, or exchange keys to the repository. The backend connects to PostgreSQL using the database connection string.
7. Deploy, then verify `/api/health` and test opening and closing a paper trade. Restart the Render service and confirm the journal remains present.

The backend creates its PostgreSQL tables on startup. Existing SQLite files are not automatically migrated; this deployment path starts with a new Supabase database unless you separately export and import existing data. Use the Session Pooler URI and keep SSL enabled. Paper trading remains simulated only; no exchange private keys or live-order endpoints are added.

**Free-tier note:** Render's free web service may sleep when idle. Supabase Free projects can pause after a period of inactivity. This setup is suitable for testing, not guaranteed always-on operation. Review current provider limits before relying on it.
