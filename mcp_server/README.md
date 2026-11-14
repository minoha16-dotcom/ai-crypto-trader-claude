# AI Crypto Trader — private ChatGPT MCP bridge

This MCP server exposes the existing FastAPI backend as ChatGPT tools. It is
intended for one user's private development setup.

## Safety boundary

- Public exchange market data only; no exchange API keys or private account endpoints.
- No live order placement, cancellation, transfers or withdrawals.
- `open_paper_trade` writes a simulated position to the backend's configured database (SQLite locally, PostgreSQL on the Render deployment).
- `close_paper_trade` closes a simulated position at the exit price supplied by the user.
- `get_ai_recommendations` ranks scanner setups against local closed paper-trade outcomes. It returns suggestions only and never opens a trade.
- Spot SHORT is rejected by the backend.
- Futures simulation remains simplified; it does not fully model contract sizing,
  margin, funding, slippage or liquidation.
- Do not expose the unauthenticated FastAPI backend or MCP listener directly to
  the public internet. Keep both bound to loopback and use an approved private
  MCP tunnel for ChatGPT connectivity.

## Install and run on Windows PowerShell

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-mcp.txt
```

Terminal 1 — start the trader API:

```powershell
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Terminal 2 — start the MCP server:

```powershell
$env:TRADER_API_URL = "http://127.0.0.1:8000"
$env:MCP_PORT = "8001"
python -m mcp_server.server
```

The Streamable HTTP endpoint is:

```
http://127.0.0.1:8001/mcp
```

### Use the authenticated Render API as the backend

The MCP process can remain local/private while calling the deployed FastAPI service. When `scan_opportunities` or `get_ai_recommendations` is called without an exchange argument, the bridge reads `default_exchange` from `/api/exchanges`; explicit exchange arguments continue to take precedence:

```powershell
$env:TRADER_API_URL = "https://ai-crypto-trader-claude.onrender.com"
$env:TRADER_API_USERNAME = "<your Render dashboard username>"
$env:TRADER_API_PASSWORD = "<your Render dashboard password>"
python -m mcp_server.server
```

Supply the credentials through your local environment or a secret manager; do not commit them. Set both variables or neither. Keep the MCP listener bound to loopback and use the private tunnel for ChatGPT.

## ChatGPT availability and deployment status

This repository currently contains a local/private MCP bridge, not a hosted
public MCP endpoint. Do not publish the unauthenticated local listener or place
it behind an unauthenticated public URL.

OpenAI's current custom MCP documentation lists full custom MCP support,
including write tools, for ChatGPT Business and Enterprise/Edu workspaces.
Pro is limited to read/fetch permissions; Go is not listed as eligible for
custom MCP connections in that documentation. Confirm plan/workspace
eligibility before attempting to connect.

A hosted connection also requires an HTTPS endpoint and a supported,
production-grade authentication flow (normally OAuth with refresh tokens).
This bridge does not implement an OAuth authorization server, so it is not
ready to expose publicly. This is intentionally not described as a completed
hosted integration.

## Connect ChatGPT privately

ChatGPT does not connect directly to a localhost URL. A Secure MCP Tunnel may
provide private transport where available, but it does not bypass plan
eligibility or the need to authenticate and protect the server.

If a secure tunnel or a plan-eligible custom MCP connection is unavailable,
do not expose this development server publicly. Production hosting requires
HTTPS, OAuth, access controls, rate limits and a hardened backend. These
components are not claimed as complete in this release.

In ChatGPT web, add a custom MCP server/plugin using the tunnel connection,
scan the tools, and enable only the tools you intend to use. Keep the app
private; do not publish it to a public directory.

## Smoke test prompts

- "Check the AI Crypto Trader backend status."
- "Get the public BTC/USDT ticker from OKX Spot."
- "Scan BTC/USDT and ETH/USDT on OKX Spot using 1h candles."
- "Show my paper account, risk limits, and open positions."
- "Show the latest trading opportunities."
- "Get KuCoin BTC/USDT Spot instrument precision and order limits."
- "Backtest BTC/USDT Futures on OKX using 4h candles with Volume Ratio thresholds 0.8, 1.0, and 1.2."
- "Open a paper LONG on BTC/USDT with entry 60000 and stop 59000."
- "Close paper trade 1 at exit price 61000."

For every paper trade, review the exchange, symbol, market type, side, entry,
stop and optional target before asking ChatGPT to call the write tool.

## Important version note

This MCP bridge is an integration layer, not a new trading engine. The project
release is read from the root VERSION file and checked against the Claude plugin
manifests and README in CI. The bridge calls the FastAPI endpoints for market data,
instrument metadata, scanning, backtesting, opportunities, open positions, risk
and the paper journal. It uses
the same configured backend database and does not place exchange orders.

