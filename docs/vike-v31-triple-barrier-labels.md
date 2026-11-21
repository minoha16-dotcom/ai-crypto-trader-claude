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


## Binance USD-M liquidation verification and historical Mark Price coverage

The independent verifier in `backend/binance_usdm_liquidation.py` implements
Binance's published isolated one-way USDⓈ-M liquidation-price equation using
an explicit maintenance-bracket schedule (MMR and cumulative maintenance
amount). It solves each bracket and accepts exactly one self-consistent
solution. Cross margin, hedge mode, portfolio offsets, multi-assets collateral,
and dynamic maintenance-bracket changes during a position are unsupported and
fail closed. This is a research calculation, not a replacement for Binance's
risk engine; bracket schedules must be sourced and versioned for the symbol
and effective date.

The script `scripts/audit_binance_usdm_mark_price.py` audits each symbol over
an exact half-open UTC window. Both trade and Mark Price CSVs must contain
every expected 15-minute candle, with exact open and close timestamps, valid
OHLC, no duplicates, no missing bars and no extras. It emits a per-symbol
report and an aggregate manifest; any failed symbol fails the entire window.

Label generation now requires both `mark_price_coverage_verified=True` and a
PASS result from the independent liquidation verifier whose calculated price
matches the supplied liquidation threshold. A caller cannot enable labeling
merely by supplying a Mark Price CSV or a guessed liquidation price.

Example coverage manifest:

```json
{
  "symbols": [
    {
      "symbol": "BTCUSDT",
      "trade_csv": "data/BTCUSDT-trade_15m.csv",
      "mark_csv": "data/BTCUSDT-mark_15m.csv"
    }
  ]
}
```

Run the audit with `--start` and `--end` aligned to UTC 15-minute
boundaries. A PASS means exact candle coverage for the supplied files/window;
it does not prove that Binance's historical archive is complete beyond those
files or validate the provenance of the bracket schedule.
