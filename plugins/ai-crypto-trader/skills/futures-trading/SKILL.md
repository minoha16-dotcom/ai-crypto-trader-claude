---
name: futures-trading
description: Analyze crypto perpetual and dated futures, including funding, open interest, basis, leverage, margin, liquidation, and contract risks.
---
# Futures Trading Research
Require exchange, canonical contract symbol, linear/inverse type, margin mode, leverage, side, entry, stop, and contract specification. Check funding interval, open interest, basis, volume, and liquidation data only when sourced and timestamped. Do not treat funding or open interest alone as directional signals.

The backend paper simulator currently models isolated margin only. It uses leverage capped by MAX_FUTURES_LEVERAGE, a configurable assumed funding rate and interval, a fixed maintenance-margin ratio and liquidation fee, and an estimated liquidation trigger. Its funding and liquidation calculations are approximations, not venue-equivalent. It does not load exchange maintenance tiers, mark-price rules, bankruptcy prices, actual funding history, collateral conversion or order-book liquidity. Inverse contract collateral is a quote-equivalent approximation. Never present its liquidation price as an exchange-reported value; do not infer live liquidation prices without authoritative venue data.

For research with this simulator, disclose leverage, funding rate and interval, maintenance rate, liquidation fee, contract size, and model limitations. Compare the result with Spot and unleveraged Futures. Never recommend high leverage or invent external data. No live orders or credential collection.