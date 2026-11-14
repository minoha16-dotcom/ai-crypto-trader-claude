# AI Crypto Trader V2

Release: 2.1.0 (canonical version: root VERSION file)

An AI-assisted cryptocurrency research and paper-trading toolkit with a Python/FastAPI engine, a Claude Code plugin containing 12 Skills, and an optional private ChatGPT MCP bridge.

> **Safety boundary:** Paper trading only. Exchange adapters use public market data. The project does not connect to private exchange account endpoints, submit live orders, cancel orders, transfer funds, or withdraw assets. Nothing here is financial advice.

## Supported markets and exchanges

- New market scans and paper trades: **KuCoin, Bybit, and OKX**.
- Market types: **Spot and Futures** (Futures simulation has limitations described below).
- Binance is retained only as a legacy public-data adapter for existing persisted paper positions. It is not a selectable default for new scans or paper trades.

## The twelve Claude Skills

1. **crypto-market-analysis** — market structure and asset context.
2. **technical-analysis** — EMA, RSI, ATR, ADX, and volume-ratio analysis aligned with backend definitions.
3. **onchain-analysis** — blockchain activity and on-chain metrics when reliable data is supplied.
4. **crypto-news** — news context and event awareness.
5. **risk-management** — risk budgets, exposure, and trade invalidation.
6. **spot-trading** — Spot-market research workflows.
7. **futures-trading** — Futures-market research workflows.
8. **market-regime** — trending, ranging, and changing market conditions.
9. **derivatives-analysis** — derivatives metrics and positioning context.
10. **portfolio-allocation** — allocation and portfolio exposure considerations.
11. **trade-journal** — recording and reviewing simulated trades.
12. **strategy-backtesting** — historical strategy evaluation and walk-forward research guidance.

Use the Skills as research assistance, not as autonomous trading signals or a guarantee of returns.

## Install the Claude plugin or Skills

### Claude Code plugin

In Claude Code, run:

~~~text
/plugin marketplace add minoha16-dotcom/ai-crypto-trader-claude
/plugin install ai-crypto-trader@ai-crypto-trader-marketplace
~~~

Example namespaced Skills and commands:

~~~text
/ai-crypto-trader:crypto-market-analysis
/ai-crypto-trader:technical-analysis
/ai-crypto-trader:risk-management
/ai-crypto-trader:analyze BTC/USDT Spot 1h
/ai-crypto-trader:risk-check BTC/USDT LONG entry=65000 stop=64000
~~~

### Plain Claude Code Skills

The Markdown Skill folders can also be copied without installing the plugin.

macOS / Linux:

~~~bash
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
mkdir -p ~/.claude/skills
cp -R ai-crypto-trader-claude/plugins/ai-crypto-trader/skills/* ~/.claude/skills/
~~~

Windows PowerShell:

~~~powershell
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
New-Item -ItemType Directory -Force "$HOME\.claude\skills"
Copy-Item "ai-crypto-trader-claude\plugins\ai-crypto-trader\skills\*" "$HOME\.claude\skills" -Recurse -Force
~~~

Plain Skills do not require Python packages. In a normal chat, pasting a Skill does not provide the dashboard, exchange-data backend, or persistent paper-trading database.

## Architecture: backend, Skills, and MCP

These components have separate responsibilities; they are not interchangeable:

- **Backend (FastAPI):** authoritative implementation for public market data, indicator calculations, central risk checks, paper-trade state, journal, and the implemented single-window backtest plus rolling/expanding walk-forward evaluation.
- **Claude Skills/plugin:** reusable natural-language analysis instructions and workflow guidance. Skills do not independently fetch exchange data, change server settings, or write to the paper journal.
- **MCP bridge:** exposes selected backend endpoints as tools to an MCP-compatible client. The private ChatGPT integration uses the MCP bridge to call the same backend. MCP is a tool adapter, not a Skill runtime or a second strategy engine.

A connected client can use the MCP tools while following its own Skill instructions. The project does not claim that Claude Skills are automatically loaded or executed inside ChatGPT.

See [README_CHATGPT_MCP.md](README_CHATGPT_MCP.md) and [mcp_server/README.md](mcp_server/README.md) for the private MCP setup. Keep the MCP listener private; this repository does not include a public OAuth server.

## Optional Python dashboard and API

The backend is independent of the Claude plugin and Skills.

### Requirements

- Python 3.11+
- Git and pip
- Dashboard username and password (required at startup)

### Install and run locally

~~~powershell
git clone https://github.com/minoha16-dotcom/ai-crypto-trader-claude.git
cd ai-crypto-trader-claude
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
~~~

Edit .env and set non-empty DASHBOARD_USERNAME and DASHBOARD_PASSWORD values. Review the paper-risk settings before starting.

~~~bash
uvicorn backend.main:app --reload
~~~

Dashboard: http://127.0.0.1:8000

Health: http://127.0.0.1:8000/api/health

Readiness: http://127.0.0.1:8000/api/ready

The dashboard and API routes require HTTP Basic Authentication. Only health and readiness checks are public. Interactive API documentation is disabled.

## Configuration

The canonical example is .env.example. Values below are defaults; environment variables or a local .env file can override them. DEFAULT_EXCHANGE is validated against KuCoin, Bybit, and OKX and supplies the default for the dashboard, API scan/backtest/recommendation routes, and MCP scan/recommendation tools when no exchange is passed. An explicit supported exchange always takes precedence.

| Setting | Default | Purpose |
|---|---:|---|
| DEFAULT_EXCHANGE | kucoin | Must be kucoin, bybit, or okx |
| BYBIT_API_HOSTNAME | bybit.com | Official Bybit global API host: bybit.com or api.bytick.com (set the hostname as bytick.com) |
| DEFAULT_MARKET_TYPE | spot | Default market type |
| MAX_OPEN_TRADES | 3 | Maximum number of simultaneous simulated positions |
| REWARD_RISK_RATIO | 2.0 | Default target distance relative to stop distance |
| RISK_PER_TRADE_PCT | 0.5 | Per-trade risk budget |
| MAX_OPEN_RISK_PCT | 2.0 | Aggregate open-risk cap |
| MAX_DAILY_LOSS_PCT | 3.0 | Daily realized-loss cap |
| MAX_POSITION_NOTIONAL_PCT | 20.0 | Maximum position notional |
| SIGNAL_MIN_ADX | 20.0 | Minimum ADX filter |
| SIGNAL_MIN_VOLUME_RATIO | 1.0 | Default Volume Ratio filter |
| SIGNAL_MIN_ATR_PCT | 0.1 | Minimum ATR percent filter |
| TAKER_FEE_RATE | 0.0006 | Estimated fee per side |
| SLIPPAGE_BPS | 2.0 | Estimated slippage per side |
| POSITION_MONITOR_MAX_PRICE_AGE_SECONDS | 120 | Reject timestamped ticker snapshots older than this age when auto-closing paper positions |
| MAX_FUTURES_LEVERAGE | 5 | Maximum leverage accepted by the Futures paper simulator |
| MAX_FUTURES_MARGIN_PCT | 50 | Maximum account equity reserved as aggregate Futures initial margin |
| PAPER_FUTURES_DEFAULT_FUNDING_RATE | 0.0001 | Assumed funding payment rate per settlement; not a live venue feed |
| PAPER_FUTURES_FUNDING_INTERVAL_HOURS | 8 | Assumed interval between funding settlements |
| PAPER_FUTURES_MAINTENANCE_MARGIN_RATE | 0.005 | Fixed maintenance-margin ratio used by the simplified liquidation estimate |
| PAPER_FUTURES_LIQUIDATION_FEE_RATE | 0.005 | Assumed paper liquidation fee rate |

The default open-position limit remains 3 and the default reward/risk ratio remains 2.0; these values are now centralized in Settings and are not hidden constants.

## Indicators and signal rules

The backend currently calculates:

- EMA 20 and EMA 50.
- RSI 14.
- ATR 14 and ATR percent.
- ADX 14.
- Volume Ratio: latest closed-candle volume divided by the mean volume of the preceding 20 closed candles.

The directional score uses three votes: EMA 20 versus EMA 50, RSI zone (55/45), and close versus EMA 20. ADX, ATR percent, and Volume Ratio are filters, not extra directional votes. EMA alignment and price relative to EMA 20 may be correlated and should not be treated as independent confirmations.

MACD and EMA 200 are not part of the current backend signal engine. The Technical Analysis Skill distinguishes optional external chart analysis from backend-generated metrics.

## Backtesting and Volume Ratio experiments

The current GET /api/backtest endpoint implements a single chronological OHLCV simulation. Signals use closed candles and enter at the next candle open. The simulation applies fees and slippage, assumes the stop first when stop and target both occur inside one candle, handles adverse stop gaps, and reports open-at-end positions separately.

For a controlled Volume Ratio experiment, run the same exchange, symbol, market type, timeframe, candle range, balance, and risk settings while changing only the threshold:

~~~text
GET /api/backtest?exchange=kucoin&symbol=BTC/USDT&market_type=spot&timeframe=1h&limit=1000&volume_ratio_threshold=0.8
GET /api/backtest?exchange=kucoin&symbol=BTC/USDT&market_type=spot&timeframe=1h&limit=1000&volume_ratio_threshold=1.0
GET /api/backtest?exchange=kucoin&symbol=BTC/USDT&market_type=spot&timeframe=1h&limit=1000&volume_ratio_threshold=1.2
~~~

Omitting the override uses SIGNAL_MIN_VOLUME_RATIO. The same optional parameter is available on the MCP run_backtest tool. The MCP run_walk_forward tool exposes the chronological evaluation endpoint and its fold/window controls.

## Isolated Futures paper simulation

Futures paper positions now accept leverage, a funding rate per settlement, and a funding interval. The default is 1x leverage, a 0.01% assumed funding rate every 8 hours, a fixed 0.5% maintenance-margin ratio, and a 0.5% liquidation fee. Leverage is capped by MAX_FUTURES_LEVERAGE (5x by default), and aggregate initial margin is capped by MAX_FUTURES_MARGIN_PCT (50% of account equity by default). Spot remains unleveraged and does not accrue funding.

The model calculates initial margin as notional divided by leverage. Funding is signed: a positive rate is charged to LONG and credited to SHORT; a negative rate reverses that direction. In live paper monitoring, any missed funding settlements are accrued at the currently observed notional because historical marks for each missed settlement are unavailable. In OHLCV backtests, funding is accrued on the first candle at or after each interval using that candle's open. If several intervals were missed, the same candle open is used for each missed event.

Liquidation uses an estimated isolated-margin threshold with a fixed maintenance rate and configured fee assumptions. In bar backtests, liquidation is checked before stop/target, and the liquidation threshold is assumed to have been touched if the candle's adverse extreme crosses it. This is deliberately conservative where OHLCV cannot reveal intrabar ordering.

This is **not an exchange-equivalent liquidation engine**. It does not load venue-specific maintenance-margin tiers, risk limits, mark-price rules, bankruptcy prices, auto-deleveraging, funding history, insurance-fund rules, collateral conversion, or order-book fills. Inverse contracts use a quote-equivalent approximation rather than an exchange's full coin-collateral account model. The estimate can differ materially from KuCoin, Bybit, or OKX. Never use this simulator to size a live position or infer a venue's actual liquidation price.

## Walk-Forward evaluation

The backend exposes `GET /api/walk-forward`, and the private MCP bridge
exposes `run_walk_forward`. The runner evaluates the current fixed strategy in
rolling or expanding chronological folds and reports per-period and aggregate
metrics. It supplies warm-up-only indicator history and enforces bounded
trade-horizon controls:

- `max_holding_bars=20`: a trade exits at the close of the bar that reaches
  the configured maximum, unless its stop or target exits first.
- `purge_bars` must be at least the maximum holding horizon (default 20).
- `embargo_bars` defaults to `max(warmup_bars, max_holding_bars)` (50 bars
  with the default warm-up) and rejects a smaller explicit value.
- Any position still open at a segment boundary is closed at the final candle
  close with modeled fees and slippage; no open mark-to-market P&L carries
  across periods. Breakeven trades are reported separately from losses.

Example request:

~~~text
GET /api/walk-forward?exchange=kucoin&symbol=BTC/USDT&market_type=spot&timeframe=1h&limit=1500&train_bars=250&validation_bars=100&test_bars=100&step_bars=100&warmup_bars=50&purge_bars=20&embargo_bars=50&max_holding_bars=20&window_mode=rolling&max_folds=10
~~~

For a repeatable multi-market research report, run the pre-registered runner
against public KuCoin and OKX data:

~~~powershell
$env:STRATEGY_VALIDATION_EXCHANGES = "kucoin,okx"
$env:STRATEGY_VALIDATION_LIMIT = "5000"
python scripts/run_strategy_validation.py
~~~

It evaluates Spot BTC/USDT and ETH/USDT plus Futures BTC/USDT, where available,
using chronological 1h folds. It compares Volume Ratio thresholds 0.8, 1.0,
and 1.2 and runs three pre-registered directional-vote ablations at threshold
1.0: remove EMA20/EMA50 alignment, remove close/EMA20, and collapse the two
correlated trend votes into one composite vote. The report includes per-fold
out-of-sample trades, net return, expectancy, profit factor, drawdown, a
long-only buy-and-hold reference, dataset coverage, and explicit limitations.
The JSON and Markdown reports are written to `artifacts/`; GitHub Actions
uploads them as a workflow artifact. The research workflow completing does not
mean the strategy passed: inspect `evidence_status` and all per-market folds.
A small or incomplete sample is marked inconclusive.

The holding limit and forced boundary exit are walk-forward evaluation
assumptions; the ordinary `/api/backtest` default is unchanged. These controls
bound the configured feature lookback and trade horizon, but do not constitute
a formal proof against every possible source of data leakage. The runner uses
fixed backend Settings; it does not optimize parameters or select a threshold
using validation data. Each period starts with an independent balance, so
fold P&L is not a continuous portfolio simulation. Strategy validation still
requires reviewing pre-registered folds, sensitivity checks, and
correlated-indicator ablations. See
[docs/strategy-validation.md](docs/strategy-validation.md) for methodology,
API parameters, limitations, and acceptance criteria.

## MCP: optional private ChatGPT integration

Install the MCP-specific dependency set separately from the backend:

~~~powershell
pip install -r requirements-mcp.txt
~~~

Terminal 1 — start FastAPI:

~~~powershell
uvicorn backend.main:app --host 127.0.0.1 --port 8000
~~~

Terminal 2 — start the local MCP bridge:

~~~powershell
$env:TRADER_API_URL = "http://127.0.0.1:8000"
$env:TRADER_API_USERNAME = "<same value as DASHBOARD_USERNAME in .env>"
$env:TRADER_API_PASSWORD = "<same value as DASHBOARD_PASSWORD in .env>"
python -m mcp_server.server
~~~

The Streamable HTTP endpoint is http://127.0.0.1:8001/mcp. The MCP process must pass the same dashboard credentials as the protected FastAPI backend. To call the Render backend instead, set TRADER_API_URL, TRADER_API_USERNAME, and TRADER_API_PASSWORD in the local MCP process environment. Do not commit credentials or expose an unauthenticated listener publicly.

MCP write tools are paper-only: open_paper_trade creates a simulated row in the configured database and close_paper_trade records a user-supplied simulated exit price. Neither tool submits an exchange order.

## Dependencies and tests

Dependency sets are intentionally separated:

- requirements.txt — production backend only; used by Render.
- requirements-test.txt — pytest, HTTP client, and MCP modules needed by the test suite.
- requirements-mcp.txt — optional MCP bridge packages for local/private ChatGPT connectivity.

Run the full test suite:

~~~powershell
pip install -r requirements-test.txt
python -m pytest -q
~~~

Public exchange smoke checks:

~~~powershell
python scripts/check_exchanges.py
~~~

For a focused local Bybit check (Spot and Futures only), you can use the
official alternate global REST host when the default host is rate-limited:

~~~powershell
$env:BYBIT_API_HOSTNAME = "bytick.com"
$env:SMOKE_REQUEST_PAUSE_SECONDS = "15"
python scripts/check_bybit.py
~~~

The supported host values are `bybit.com` and `bytick.com`. These scripts use
public market-data endpoints only; no API keys or orders are involved.
HTTP 429, regional blocks, and cooldowns remain inconclusive, not successful
connectivity checks. Run the focused check from the network that will use
Bybit; GitHub-hosted runner results do not prove connectivity from a local
machine.

## Render deployment

The repository Blueprint is defined by render.yaml:

- Branch: deploy/render-supabase-postgres.
- Build installs requirements.txt only (production dependencies).
- Health check: /api/ready.
- Auto-deploy trigger: off in the Blueprint.
- Required private environment values: DATABASE_URL, DASHBOARD_USERNAME, DASHBOARD_PASSWORD.
- PYTHON_VERSION is pinned in render.yaml.

Use the Supabase PostgreSQL Session Pooler URI for DATABASE_URL and keep it private. Set a unique dashboard username and password in Render. After a manual deploy, verify /api/health and /api/ready, authenticate to the dashboard, and confirm the journal remains available. Check the actual Render service's Auto-Deploy setting as well; a Blueprint file alone does not retroactively change an existing service setting.

The Render Free web service spins down after 15 consecutive minutes without inbound HTTP requests or WebSocket messages; the next request wakes it and may incur roughly a one-minute cold start. Its filesystem is ephemeral, so do not store the database on the service disk. Supabase Free projects can pause after a week with too little database activity. This setup is for testing and research, not guaranteed always-on monitoring. The server-side paper position monitor is best-effort and periodic, not real-time or guaranteed. It is not an exchange stop-loss service.

## Fine print

- Public data only: never add exchange API keys, private account credentials, or withdrawal permissions.
- Futures are isolated-margin paper simulations with fixed assumed funding, maintenance-margin and liquidation-fee rates. Venue-specific margin tiers, mark prices, bankruptcy prices, and full inverse coin-collateral accounting are not modeled.
- The paper monitor checks public last-traded prices periodically while the host is awake. Gaps and price reversals between polls can produce different results from stop/target or estimated liquidation levels.
- Backtests are historical bar simulations, not forecasts; their funding schedule uses candle opens and their liquidation checks use OHLC extremes, not exchange mark-price feeds or exact fills.
- Advisor historical scores come only from user-selected closed paper trades linked to scanner signals. Unopened signals are absent, so selection bias is possible. Chronological Brier diagnostics are not calibrated probabilities or proof of predictive edge.
- Risk controls do not eliminate market, software, or execution risk.
- Local development uses SQLite; Render uses PostgreSQL. Back up the PostgreSQL database and test restore procedures.
- Dashboard credentials are mandatory. Never commit .env or put credentials in issues, chat, or source control.
- Not financial advice. Make independent decisions and comply with applicable laws and exchange terms.

## Files

~~~
.
├── .claude-plugin/
│   └── marketplace.json
├── .github/workflows/
│   ├── tests.yml
│   ├── exchange-smoke.yml
│   └── supabase-*.yml
├── backend/
│   ├── config.py
│   ├── indicators.py
│   ├── backtest.py
│   ├── walk_forward.py
│   └── version.py
├── docs/
│   └── strategy-validation.md
├── mcp_server/
├── plugins/ai-crypto-trader/
│   ├── .claude-plugin/plugin.json
│   └── skills/
├── tests/
├── VERSION
├── requirements.txt
├── requirements-test.txt
├── requirements-mcp.txt
├── .env.example
├── render.yaml
└── README.md
~~~

## License

MIT. See LICENSE.
