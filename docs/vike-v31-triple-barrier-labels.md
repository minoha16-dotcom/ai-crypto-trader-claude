# Vike V3.1 — Triple-Barrier Label Engine (first vertical slice)

Status: **implementation draft; not yet accepted for historical labeling**.

Module: `backend/triple_barrier_labels.py`

## Contract

- Event time is the close timestamp of the signal candle; entry is the first
  supplied bar whose open timestamp is later than the event time.
- Entry fill applies adverse slippage. Barriers are measured from that fill:
  Wilder ATR(14) must be supplied by a separately validated causal feature
  pipeline; this module does not calculate ATR.
- Primary barriers: stop 1.5 × ATR, target 3.0 × ATR, maximum 48 complete
  15-minute bars.
- Trade OHLC is used for barrier touches. If SL and TP both touch in one bar,
  SL is selected first.
- A time exit uses the close of the 48th complete bar. A shorter horizon is
  excluded as immature.
- Taker fees are charged on entry and exit notional. Adverse slippage is
  applied to both fills. Funding cashflow uses the actual supplied settlement
  timestamp, rate and mark price; positive rates cost LONG and benefit SHORT.
- If a funding settlement falls inside the exit bar of an intrabar barrier
  exit, the ordering cannot be inferred from OHLC, so the sample is excluded.
- Missing funding coverage or unspecified fee/slippage assumptions fails
  closed. No values are forward-filled or synthesized.

## Important limitations (not approval)

This first slice does not yet:
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
