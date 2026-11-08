# AI Crypto Trader for Claude

Claude plugin marketplace for crypto research: technical analysis, on-chain research, news, Spot/Futures planning, and risk management.

> **Safety:** Research-only starter. It does not place live orders, manage exchange accounts, or withdraw funds. Not financial advice.

## Repository contents
- `.claude-plugin/marketplace.json` — marketplace manifest
- `plugins/ai-crypto-trader/` — Claude plugin, commands, and seven Skills
- `backend/` — early Python prototype for public market data, risk calculations, paper trading, and a FastAPI health endpoint

## Install in Claude
1. Open **Customize → Plugins → Add → Add marketplace**.
2. Enter `minoha16-dotcom/ai-crypto-trader-claude`.
3. Select **AI Crypto Trader** from the marketplace.

## Limitations
The backend is not yet connected to the plugin through MCP and does not implement live Spot/Futures execution. Never commit API keys or exchange secrets. Start with read-only credentials if integrations are added later.

## License
MIT.
