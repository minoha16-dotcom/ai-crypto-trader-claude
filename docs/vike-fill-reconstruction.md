# Vike fill reconstruction engine

This accounting-first module rebuilds Hyperliquid wallet × asset position
lifecycles from Vike fills. It is not a strategy, signal generator, or model
approval mechanism.

Rules:
- Preserve one row per wallet fill. A counterparty row with the same trade ID
  belongs to a different wallet and must not be removed.
- Detect duplicate wallet fills by wallet, asset, fill timestamp, and trade ID.
- Reconcile signed fill size against start_position. A mismatch makes the
  lifecycle incomplete.
- Support opens, adds, partial reductions, full closes, and reversals.
- Use closed_pnl as the venue-reported realized component; subtract separately
  reported fees only when fee_token is USD or USDC.
- A first observed non-flat position is a carry-in. It can be audited but is
  never labelled COMPLETE because its original entry was not observed.
- For a reversal, allocate the fill fee pro rata between the closing and new
  opening quantities.

COMPLETE means the position state was observed from flat to flat and supported
fee accounting was available. It does not prove a stop-loss, risk amount,
R-multiple, strategy attribution, or profitability. Those require separate
evidence. Open positions are reported separately and never labelled closed.
