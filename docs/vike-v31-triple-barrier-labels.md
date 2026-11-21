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
  SL is selected first.
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
- calculate Wilder ATR or causal features;
- validate Mark Price bar coverage or model liquidation/maintenance margin;
- model order-book spread/depth, partial fills, or contract quantity precision;
- construct overlap-controlled datasets or train any model.

Therefore the function must only be used with test fixtures until the upstream
coverage, cost assumptions, and Mark Price/liquidation policy are separately
verified. Its output is not an accepted production label set.

## Tests

```powershell
python -m pytest -q tests/test_triple_barrier_labels.py
```
