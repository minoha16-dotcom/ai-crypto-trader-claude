# AI Crypto Trader — private ChatGPT MCP bridge

This MCP server exposes the existing FastAPI backend as ChatGPT tools. It is
intended for one user's private development setup.

## Safety boundary

- Public exchange market data only; no exchange API keys or private account endpoints.
- No live order placement, cancellation, transfers or withdrawals.
- `open_paper_trade` writes a simulated position to the backend's configured database (SQLite locally, PostgreSQL on the Render deployment).
- `close_paper_trade` closes a simulated position at the exit price supplied by the user.
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
pip install -r requirements.txt
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

The MCP process can remain local/private while calling the deployed FastAPI service:

```powershell
$env:TRADER_API_URL = "https://ai-crypto-trader-claude.onrender.com"
$env:TRADER_API_USERNAME = "<your Render dashboard username>"
$env:TRADER_API_PASSWORD = "<your Render dashboard password>"
python -m mcp_server.server
```

Supply the credentials through your local environment or a secret manager; do not commit them. Set both variables or neither. Keep the MCP listener bound to loopback and use the private tunnel for ChatGPT.

## Connect ChatGPT privately

ChatGPT does not connect directly to a localhost URL. Use OpenAI's Secure MCP
Tunnel where available; it forwards requests to the local MCP server without
opening an inbound public port. Follow the current Secure MCP Tunnel guide and
use the tunnel option when adding a custom MCP server in ChatGPT.

If a secure tunnel is not available to your account, do not expose this
unauthenticated development server publicly. A production deployment needs
HTTPS, authentication (prefer OAuth), access controls, rate limits and a
hardened backend.

In ChatGPT web, add a custom MCP server/plugin using the tunnel connection,
scan the tools, and enable only the tools you intend to use. Keep the app
private; do not publish it to a public directory.

## Smoke test prompts

- "Check the AI Crypto Trader backend status."
- "Get the public BTC/USDT ticker from OKX Spot."
- "Scan BTC/USDT and ETH/USDT on OKX Spot using 1h candles."
- "Show my paper account and risk limits."
- "Open a paper LONG on BTC/USDT with entry 60000 and stop 59000."
- "Close paper trade 1 at exit price 61000."

For every paper trade, review the exchange, symbol, market type, side, entry,
stop and optional target before asking ChatGPT to call the write tool.

## Important version note

This MCP bridge is an integration layer, not a new trading engine. It calls
the existing V2.1 FastAPI endpoints and uses the same configured backend database. It does
not change the V2.1 release status or resolve outstanding exchange-geography,
security-review, or local-runtime verification work.
