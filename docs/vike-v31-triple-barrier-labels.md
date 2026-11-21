# Vike V3.1 — Triple-Barrier Label Engine (first vertical slice)

Status: **implementation draft; not yet accepted for historical labeling**.

Module: `backend/triple_barrier_labels.py`

## Contract

- Event time must be the UTC close timestamp of a 15-minute signal candle
  (open-boundary + 899,999 ms); entry is the immediately following 15-minute
  bar open. Bar open timestamps must align to UTC 15-minute boundaries.
- Entry fill applies adverse slippage. Barriers are measured from that fill:
  Wilder ATR(14) must be supplied by a separately validated causal feature
  pipeline; this module does not calculate ATR.
- Primary barriers: stop 1.5 × ATR, target 3.0 × ATR, maximum 48 complete
  15-minute bars.
- Trade OHLC is used for barrier touches. If SL and TP both touch in one bar,
  SL is selected first. Mark Price OHLC must exist for every trade candle from
  entry through exit, with exact matching timestamps; incomplete or mismatched
  Mark Price coverage excludes the event.
- The labeler requires an exchange-computed liquidation price for the exact
  position configuration. LONG is excluded if Mark Price low touches/breaches
  it; SHORT is excluded if Mark Price high touches/breaches it. This code does
  not estimate liquidation price: contract type, quantity, leverage, margin
  mode, maintenance tier, collateral, funding and liquidation fees must be
  accounted for upstream.
- A time exit uses the close of the 48th complete bar. A shorter horizon is
  excluded as immature.
- Taker fees are charged on entry and exit notional per unit. Adverse slippage
  is applied to both fills. A stop-market exit that gaps through its stop is
  filled from the bar open, then adjusted for adverse slippage; it is not
  assigned the stale stop price. A target is conservatively filled at its
  target price.
- Funding cashflow uses the supplied settlement timestamp, rate and mark
  price. Positive rates produce a negative cashflow (payment) for LONG and a
  positive cashflow (receipt) for SHORT; negative rates reverse those signs.
  The signed cashflow is added to net PnL. A settlement exactly at
  entry is ambiguous and excludes the event. Any settlement within an
  intrabar barrier-exit candle is ambiguous and excludes the event. For a
  time exit at candle close, earlier settlements in that candle are applied,
  while a settlement exactly at the close timestamp is excluded.
- Missing funding coverage or unspecified fee/slippage assumptions fails
  closed. Non-numeric and boolean OHLC/Funding inputs are rejected. No values
  are forward-filled or synthesized.

## Important limitations (not approval)

This first slice does not yet:
- model quantity, contract multiplier, inverse contracts, order-book spread/depth,
  partial fills, or exchange-specific quantity/price precision; the accounting
  is per underlying unit and must not be generalized to inverse contracts;
- download or validate the full market dataset;
- independently prove Funding archive semantic equivalence/completeness;
- model exchange-specific liquidation-price calculation, maintenance-margin
  tiers, cross-margin collateral or liquidation fees; those must be supplied
  and independently verified upstream;
- model order-book spread/depth, partial fills, or contract quantity precision;
- construct overlap-controlled datasets or train any model.

Therefore the function must only be used with test fixtures until the upstream
coverage, cost assumptions, and Mark Price/liquidation policy are separately
verified. Its output is not an accepted production label set.

## Causal ATR and Mark Price guard

`backend/causal_market_features.py` implements causal Wilder ATR: first TR is
high-low, the initial ATR is the arithmetic mean of the first 14 TR values,
and later values use the Wilder recursive update. Earlier rows are `None`;
future candles cannot affect earlier ATR values. The module also validates
exact trade/Mark Price candle alignment and checks liquidation breaches using
Mark Price lows for LONG and highs for SHORT. The actual liquidation price is
never estimated by this module.

## Tests

```powershell
python -m pytest -q tests/test_triple_barrier_labels.py
```
