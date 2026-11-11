# Use AI Crypto Trader inside ChatGPT (private MCP app)

The MCP integration is in [mcp_server/README.md](mcp_server/README.md).

It exposes market-data, scanner, risk, account and journal tools plus two
paper-trading-only write tools. It does not execute real exchange orders.

**Local services**
- FastAPI backend: `http://127.0.0.1:8000`
- MCP Streamable HTTP: `http://127.0.0.1:8001/mcp`

ChatGPT needs a remote connection path. Prefer OpenAI Secure MCP Tunnel for a
private local server; do not publish the unauthenticated development endpoint.
