---
name: technical-analysis
description: Analyze crypto charts using the same EMA, RSI, ATR, ADX, and volume-ratio definitions as the AI Crypto Trader engine.
---
# Technical Analysis

Request symbol, exchange, market type (Spot or Futures), timeframe, candle
timestamps, and OHLCV/chart input. State whether candles are closed or forming.
Never invent prices or indicator readings.

## Backend indicator contract

When using the project's backend scan or paper-backtest output, interpret only
the indicators that the engine actually calculates:

- EMA 20 and EMA 50: trend alignment.
- RSI 14: momentum; the directional score uses RSI at or above 55 for a
  positive vote and at or below 45 for a negative vote.
- ATR 14 and ATR percent: volatility; ATR percent is a minimum-quality filter.
- ADX 14: trend-strength filter, compared with the configured minimum ADX.
- Volume Ratio: volume of the latest closed candle divided by the mean volume of
  the previous 20 closed candles. The default filter threshold is 1.0 and the
  backtest can override it for controlled 0.8 / 1.0 / 1.2 experiments.

The backend's directional score is composed of three votes: EMA 20 versus EMA
50, RSI zone, and close versus EMA 20. ADX, ATR percent, and Volume Ratio are
filters; they are not extra directional votes. EMA alignment and close versus
EMA 20 may be correlated, so do not describe them as independent confirmations.

## Analysis workflow

1. Identify higher-timeframe trend and swing structure from supplied data.
2. Mark support/resistance as zones, not false-precision prices.
3. Explain each backend indicator with its period, timestamp, and value.
4. If MACD, EMA 200, or additional indicators are calculated from supplied
   external chart data, label them as external analysis; they are not currently
   part of the backend signal engine.
5. Define the setup hypothesis, trigger, invalidation, and what would weaken it.
6. Separate observations from interpretations and scenarios.

Output trend, structure, source/timestamp, the backend indicator table, any
clearly separated external indicators, setup hypothesis, invalidation, and
limitations. Never present heuristic scan output as a validated prediction.
