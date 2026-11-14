---
name: strategy-backtesting
description: Evaluate crypto strategies with chronological splits, conservative execution assumptions, and walk-forward validation design.
---
# Strategy Backtesting

Specify the strategy rules, exchange, symbol, market type, candle interval,
sample window, data source, fee, slippage, and position-sizing assumptions.

## Current backend backtest

The GET /api/backtest endpoint is a single chronological OHLCV simulation.
The GET /api/walk-forward endpoint wraps the same engine in rolling or
expanding train/validation/test folds. Each segment receives prior warm-up
candles that are not scored; test windows do not overlap. The runner defaults
to a 20-bar maximum holding horizon, requires purge at least that long, and
defaults embargo to the greater of warm-up and holding horizon (50 bars by
default). Trades still open at a segment end are closed at the final candle
close with modeled fees and slippage; no open mark-to-market P&L carries into
the next segment. The ordinary single-window backtest keeps its existing
unbounded-position default. These guards are not a formal guarantee against
every possible source of leakage. It uses closed candles, next-open entries,
configured fees and slippage, and assumes the stop occurs first when stop and
target both fall inside one bar. It does not model funding, margin, liquidation,
order-book depth, or real fills.

The optional Volume Ratio threshold can be tested at 0.8, 1.0, and 1.2 by
running the same dataset with only that threshold changed. Omitting the
override uses the Settings default.

## Current Walk-forward runner limitations

The v1 runner evaluates fixed backend settings; it does not automatically tune
parameters or select a candidate using validation data. Treat train and
validation metrics as diagnostics and the forward test as out-of-sample only
for the frozen configuration. Each fold resets its paper balance. A summed
fold P&L is not a continuous portfolio equity curve. Do not label a strategy
validated until the complete pre-registered folds and sensitivity checks have
been run and independently reviewed.

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
