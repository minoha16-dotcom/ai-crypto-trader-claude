---
name: strategy-backtesting
description: Evaluate crypto strategies with chronological splits, conservative execution assumptions, and walk-forward validation design.
---
# Strategy Backtesting

Specify the strategy rules, exchange, symbol, market type, candle interval,
sample window, data source, fee, slippage, and position-sizing assumptions.

## Current backend backtest

The implemented endpoint is a single chronological OHLCV simulation. It uses
closed candles, enters at the next candle open, applies configured fees and
slippage, assumes the stop occurs first when stop and target both fall inside
one bar, and reports open-at-end positions separately. It does not model
funding, margin, liquidation, order-book depth, or real fills. Do not describe
this endpoint as a completed walk-forward engine.

The optional Volume Ratio threshold can be tested at 0.8, 1.0, and 1.2 by
running the same dataset with only that threshold changed. Omitting the
override uses the Settings default.

## Walk-forward research workflow

1. Freeze the candidate rules and parameter grid before opening the test fold.
2. Split data chronologically into training, validation, and forward test
   windows; never shuffle candles.
3. Tune only in training and select one candidate using validation data.
4. Purge trades crossing split boundaries and apply an embargo for the feature
   lookback and maximum holding horizon.
5. Repeat over rolling or expanding folds; report each fold and the combined
   out-of-sample results, not only the best period.
6. Measure feature correlations on training data and perform ablations to
   determine whether correlated indicators add independent out-of-sample value.
7. Report trade count, exposure, net return, expectancy in R, profit factor,
   drawdown, turnover, costs, benchmark comparison, sample size, and limitations.

Follow docs/strategy-validation.md for the experiment protocol and acceptance
criteria. A backtest is a historical simulation, not a forecast or guarantee.
