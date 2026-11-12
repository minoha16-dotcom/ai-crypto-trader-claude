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
- Health check: http://127.0.0.1:8000/api/health

Interactive OpenAPI documentation is disabled by default in this paper-trading deployment.

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
curl -X POST "http://127.0.0.1:8000/api/scan?exchange=kucoin&market_type=spot&symbols=BTC/USDT,ETH/USDT&timeframe=1h"
```

## The tools

### Market data and analysis

- **CCXT** connects to public market-data endpoints for KuCoin, Bybit, and OKX. KuCoin Futures uses CCXT's separate `kucoinfutures` client; Binance is retained only to mark legacy open paper positions.
- The Spot and Futures CCXT clients for each exchange share one weighted rate limiter, so their public REST request budgets are coordinated per venue.
- Identical concurrent ticker requests are coalesced into one network request; successful ticker results are cached for 2 seconds per exchange, market type, and symbol.
- HTTP 418/429 responses activate an exchange-wide cooldown shared by Spot and Futures. The registry honors `Retry-After` headers or exchange ban timestamps, logs the header/cooldown, and blocks queued and new requests until the cooldown expires without automatic retries.
- **FastAPI** serves the backend and REST API.
- **Python standard library** calculations and CCXT candle data support the indicators and portfolio simulation.
- **EMA 20/50, RSI 14, ATR 14** provide basic trend, momentum, and volatility context.

### Risk and persistence

- Central risk checks include per-trade risk, aggregate open risk, daily realized-loss limits, and a maximum position-notional cap.
- **SQLite** is used for local development; the current Render deployment uses Supabase PostgreSQL for persistent paper trades and signals.
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

## Public exchange smoke tests

Run the public connectivity check from a network that can reach the exchange APIs:

```bash
python scripts/check_exchanges.py
```

It checks BTC/USDT Spot and a supported BTC/USDT Futures contract on KuCoin, Bybit, and OKX, including public ticker, contract type, contract size, and amount precision. It never uses API keys or submits orders. Geographic blocks and HTTP 429 cooldowns are reported as `INCONCLUSIVE`, never `PASS`; other connection or metadata errors fail the check. The GitHub workflow runs on pull requests targeting the production branch and can also be started manually.

## Futures backtesting

The backtest resolves the exchange's canonical Futures symbol and loads its contract metadata before calculating results. Linear and inverse contracts use their own contract-size and P&L formulas, exchange amount precision, min/max quantity, and notional caps. Stop-market exits account for adverse opening gaps; favorable target gaps fill at the opening price. A position still open at the end of the data is returned separately with mark-to-market unrealized P&L, while realized balance remains distinct. Drawdown uses adverse intrabar OHLC extremes. Profit Factor is `null` when there are no losing trades so the response remains valid JSON. Funding, margin, and liquidation are not simulated.

## Fine print

- **Paper trading only:** the project does not place real orders or connect to private account endpoints.
- **No credentials required:** market adapters use public data. Do not add exchange secrets to the repository.
- **Futures limitations:** Futures mode selects swap markets through CCXT; symbols, contract availability, and venue behavior vary.
- **Simulation limitations:** the server-side monitor checks public ticker prices on a polling interval and closes simulated positions when a stop or target is crossed. Dashboard closes are manual. Backtests apply per-trade risk, aggregate open-risk, UTC daily realized-loss, and notional caps. They still model one position at a time and do not model funding, margin, liquidation, order-book depth, or real fills.
- **Signals are heuristic:** indicator-based scans are not validated predictive models and can be wrong.
- **Risk controls are not a guarantee:** configured limits do not eliminate market, software, or execution risk.
- **Storage:** local development uses SQLite; Render uses PostgreSQL. Back up the PostgreSQL database and test restore procedures before relying on the journal.
- **Database readiness:** `GET /api/health` checks process liveness; `GET /api/ready` checks database connectivity and required tables. The latter is the Render Blueprint health-check path.
- **Supabase probe:** manually run `Supabase read-only connectivity probe` after adding repository secret `TRADER_DATABASE_URL` with the same Supabase PostgreSQL URI used by Render. It checks connectivity/schema in a read-only transaction; it does not migrate, truncate, or edit trades. Never put the secret in chat, issues, or source control.
- **Testing:** run `pytest -q` in the repository. GitHub Actions is configured to run the test suite on pushes and pull requests; check the Actions tab for the actual result.
- **Not financial advice:** make independent decisions and comply with applicable laws and exchange terms.

## Files

```text
.
├── .claude-plugin/
│   └── marketplace.json
├── .github/workflows/
│   ├── tests.yml
│   └── exchange-smoke.yml
├── plugins/ai-crypto-trader/
│   ├── .claude-plugin/plugin.json
│   ├── commands/
│   └── skills/
├── backend/
├── tests/
│   └── test_postgres_integration.py
├── .env.example
├── requirements.txt
├── README.md
└── LICENSE
```

## License

MIT. See [LICENSE](LICENSE).


## Render Blueprint deployment (current Supabase PostgreSQL setup)

`render.yaml` describes the current PostgreSQL deployment branch `deploy/render-supabase-postgres`. It requires `DATABASE_URL`, `DASHBOARD_USERNAME`, and `DASHBOARD_PASSWORD` to be supplied as private Render environment values. Do not use the former SQLite/persistent-disk Blueprint for the current service.

- The web service uses the Render Free plan in Frankfurt and connects to Supabase PostgreSQL. Free web services spin down after 15 minutes without inbound HTTP/WebSocket traffic; they are not suitable as the only host for continuous stop/target monitoring.
- The paid Background Worker is removed from the Blueprint. The web service uses `/api/ready` to check database connectivity, but the Free web process may sleep and cannot provide continuous stop/target monitoring.
- Set a unique `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` in Render. Both values are required; the dashboard and API are protected by HTTP Basic Authentication.
- HTTP Basic Authentication protects the dashboard and every API route except `/api/health` and `/api/ready`, which are public liveness/readiness checks. API docs are disabled.
- Scanning is manual: use the dashboard Scan button. The web process can monitor while awake. The optional GitHub Actions monitor in PR #14 can perform scheduled checks every five minutes after repository secret `TRADER_DATABASE_URL` is configured and PR #14 is merged into the default branch. Both methods are periodic; a price can cross and reverse between checks. Stop/target monitoring is not guaranteed or real-time.
- The app remains paper-only and uses public market data; it does not need exchange API keys or submit live orders.

After deployment, check `/api/health` and sign in with the username and password you configured in Render. Store those credentials in a password manager; never place them in source control.


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
7. Before merging to the linked production branch, turn Auto-Deploy Off in the existing Render service Settings. After a separately approved manual deployment, verify `/api/health` and `/api/ready`, test paper open/close behavior, and confirm the journal remains present.

The backend creates and incrementally migrates its PostgreSQL tables on startup. Existing SQLite files are not automatically migrated; export and import separately if historical local records must be retained. Use the Supabase Session Pooler URI and keep SSL enabled. `render.yaml` sets `autoDeployTrigger: off` for Blueprint-managed deployments; verify the live Render service's auto-deploy setting separately because syncing a Blueprint is an explicit configuration change. The current service is connected directly to the production branch; this repository file does not silently change the live Render service. The live service was last inspected with Auto-Deploy set to On Commit. Turn Auto-Deploy Off manually in the Render Dashboard before merging this PR; this YAML change does not modify the already-existing service setting. Paper trading remains simulated only; no exchange private keys or live-order endpoints are added.

**Free-tier note:** Render's free web service may sleep when idle. Supabase Free projects can pause after a period of inactivity. This setup is suitable for testing, not guaranteed always-on operation. Review current provider limits before relying on it.
