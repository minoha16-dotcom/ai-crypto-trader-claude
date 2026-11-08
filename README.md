# AI Crypto Trader for Claude

Claude plugin marketplace for crypto research: technical analysis, on-chain research, news, Spot/Futures planning, and risk management.

> **Safety:** Research-only plugin. It does not place live orders, manage exchange accounts, or withdraw funds. Not financial advice.

## Repository contents
- `.claude-plugin/marketplace.json` — marketplace manifest
- `plugins/ai-crypto-trader/` — Claude plugin manifest, two commands, and seven Skills

## Install in Claude
1. Open **Customize → Plugins → Add → Add marketplace**.
2. Enter `minoha16-dotcom/ai-crypto-trader-claude`.
3. Select **AI Crypto Trader** from the marketplace.

## Commands
- `/ai-crypto-trader:analyze BTC/USDT Spot 4h`
- `/ai-crypto-trader:risk-check <equity> <risk%> <entry> <stop>`

## Limitations and security
The plugin is research-only and does not include live trading, MCP exchange connectivity, or order execution. Never commit API keys or exchange secrets. Do not grant withdrawal permissions to future integrations.

## License
MIT.
