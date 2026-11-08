---
name: risk-management
description: Calculate risk per trade, position sizing, stop distance, reward-to-risk, and exposure limits for Spot and Futures.
---
# Risk Management
Required inputs: account equity, risk budget, entry, stop, instrument, contract multiplier/quantity convention, fees, and (for Futures) leverage and margin mode. Ask for missing inputs; never guess.

- Risk budget = equity × risk percentage.
- Spot quantity before fees/slippage = risk budget ÷ absolute(entry − stop).
- Linear contract quantity must use the venue's actual contract specification.
- Reward-to-risk = absolute(target − entry) ÷ absolute(entry − stop).

Adjust for fees, funding, slippage, min order size, tick/lot size, and liquidation mechanics. Leverage changes margin and liquidation sensitivity; it does not remove trade risk. Use conservative risk examples (such as 0.5–1%) only as illustrations, warn about correlated exposure, and refuse definitive sizing when stop distance or contract specs are invalid/unknown. Calculations only; never send orders.