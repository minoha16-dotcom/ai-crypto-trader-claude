# Binance USD-M Maintenance-Bracket Archive — Vike V3.1

Status: **research inventory only; not a historical liquidation certificate**

Scope: Binance USDⓈ-M **USDT-margined perpetual contracts** for
`BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, LINKUSDT`,
development window November 2023–April 2026.

This document records official Binance announcement evidence located during the
archive search. It deliberately does not infer that a month with no matching
search result had no tier change. A negative search result is not proof of
historical completeness.

## Confirmed in-scope announcements

| Effective time (UTC) | Contract(s) | Existing positions | Official source |
|---|---|---|---|
| 2023-12-24 09:35 | BTCUSDT, ETHUSDT, SOLUSDT | Not affected | [Binance 2024-01-02 announcement](https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d) |
| 2024-01-02 12:50 | BNBUSDT | Not affected | [Binance 2024-01-02 announcement](https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d) |
| 2024-05-28 10:30 | BTCUSDT, ETHUSDT | Not affected | [Binance 2024-05-28 announcement](https://www.binance.com/en/support/announcement/detail/aa735cd2d8bd4e7bb092179cf086e486) |
| 2024-08-02 06:30 | SOLUSDT | Not affected | [Binance 2024-08-02 announcement](https://www.binance.com/en/support/announcement/detail/365375ab9cda445aa025c51bbaeea88e) |
| 2024-08-22 09:30 | SOLUSDT, DOGEUSDT | Not affected | [Binance 2024-08-22 announcement](https://www.binance.com/en/support/announcement/detail/572598a13c384de6aa3c4d4dc1be5f81) |
| 2024-08-27 07:30 | XRPUSDT, BNBUSDT | Not affected | [Binance 2024-08-27 announcement](https://www.binance.com/fr/support/announcement/detail/194e244a94e9416cb400318dbed2f4b9) |
| 2024-09-17 07:30 | LINKUSDT | Not affected | [Binance 2024-09-17 announcement](https://www.binance.com/en/support/announcement/detail/a05ae5aad6e44f6c848bde0a4c94ad4b) |
| 2024-11-07 08:30 | XRPUSDT | Not affected | [Binance 2024-11-07 announcement](https://www.binance.com/en/support/announcement/detail/2c473e4efef140e8b58b950987bcc577) |
| 2024-11-13 08:50 | DOGEUSDT | Not affected | [Binance 2024-11-13 announcement](https://www.binance.com/ar-AE/support/announcement/detail/0dc37e5073504db2968a5c3e9b2655fa) |
| 2024-11-19 07:30 | ADAUSDT | Not affected | [Binance 2024-11-19 announcement](https://www.binance.com/en-AU/support/announcement/detail/2766181be07e49748736810996554c47) |
| 2025-06-17 06:30 | BTCUSDT | Affected | [Binance June 2025 announcement](https://www.binance.com/en/support/announcement/detail/e778988edec446038ad536bcb0c8d460) |
| 2025-06-20 06:30 | XRPUSDT, DOGEUSDT | Affected | [Binance June 2025 announcement](https://www.binance.com/en/support/announcement/detail/e778988edec446038ad536bcb0c8d460) |
| 2025-06-23 06:30 | SOLUSDT | Affected | [Binance June 2025 announcement](https://www.binance.com/en/support/announcement/detail/aa1d165426df4a7a84a0005a5a2f1e03) |
| 2025-08-19 06:30 | BTCUSDT, ETHUSDT | Affected | [Binance August 2025 announcement](https://www.binance.com/en/support/announcement/detail/8e428625ebeb4cc7ae8678026846095c) |

The 2023-12 and 2024-05 announcements contain before/after notional bands and
maintenance-margin rates. The June and August 2025 announcements explicitly
state that existing positions are affected. The announcement wording is
important: it determines whether a position opened before the effective time
continues using the old tier schedule or moves to the new schedule.

## Search results excluded from the in-scope inventory

These are useful negative controls. They must not be imported as tier changes
for the eight-symbol universe:

- 2023-11-06 announcement search located a USD-M tier update, but the visible
  contract list did not establish a change for any of the eight target symbols.
  This is not a proof that no other November 2023 notice exists.
- 2023-11-24 changes concerned BTCBUSD, ETHBUSD, BNBBUSD and XRPBUSD, not the
  required USDT perpetual contracts.
- 2025-02-05 included XRPUSDC and other symbols, not XRPUSDT.
- 2025-04-01 and 2025-04-27 / 2025-05-02 results located other contracts, not
  the eight target symbols.
- 2025-07-25 notice was located, but its complete contract list must be checked
  against the target set before it can be marked as a negative month.
- 2025-11-28 notice listed VFYUSDT, BASUSDT, ZKJUSDT, LYNUSDT, LABUSDT, PTBUSDT, PROMPTUSDT, RECALLUSDT, QUSDT, VINEUSDT, CROSSUSDT, AIOTUSDT, STBLUSDT, 4USDT, FLOCKUSDT, ORDERUSDT, CARVUSDT, VVVUSDT, TRUTHUSDT, RIVERUSDT, XANUSDT, 币安人生USDT, POPCATUSDT, DRIFTUSDT, SAFEUSDT, ZETAUSDT and FARTCOINUSDT; none is in the target set. Existing positions are explicitly affected.\n- 2025-12-05 and 2025-12-12 surfaced notices listed other contracts, not the target eight.
- 2026-01-12, 2026-01-30, 2026-02-03, 2026-02-27, 2026-03-07,
  2026-03-10, 2026-03-20 and 2026-04-10 results were checked. The surfaced
  tier updates named other contracts, not the eight target symbols. The
  2026-04-10 notice named XPLUSDT, FFUSDT, 0GUSDT and WIFUSDT.

Portfolio Margin collateral-ratio changes are not USD-M maintenance-bracket
changes. COIN-M, USDC-M, BUSD contracts, isolated spot margin and quarterly
delivery contracts are outside this archive.

## Exchange mechanics relevant to the label engine

Binance's USDⓈ-M liquidation FAQ defines maintenance margin using position
notional, the maintenance-margin rate and the maintenance amount (the
`cum`/deduction component in the tier schedule). It also explains that
isolated mode uses the isolated wallet balance and the applicable tier values
in the liquidation calculation:
[Binance — How to Calculate Liquidation Price of USDⓈ-M Futures Contracts](https://www.binance.com/en-IN/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93).

Binance's Funding FAQ states that funding is settled at contract-specific
times, that the default interval may be changed, and that when the Futures
Account available balance is insufficient, funding can be deducted from
position margin and affect liquidation price:
[Binance — Introduction to Binance Futures Funding Rates](https://www.binance.com/en/support/faq/detail/360033525031).

Therefore the pipeline must use the recorded funding timestamps rather than
assuming every symbol always settles on an eight-hour schedule. The current
research pipeline deliberately excludes events with a funding settlement
during the open position because it does not yet recalculate isolated wallet
balance and liquidation threshold after each settlement. This is conservative,
not a claim that funding has no effect.

## Tier values extracted from official notices — partial evidence

The following low-notional bands were directly visible in official Binance
announcement content during this review. These are useful anchors for the
synthetic account, whose maximum notional is 30,000 USDT, but they are **not**
a complete tier archive. The applicable second and higher bands must also be
retained whenever the simulated notional can reach them.

| Effective UTC | Symbol | New first band (notional USDT) | MMR | Existing positions | Evidence |
|---|---|---:|---:|---|---|
| 2023-12-24 09:35 | BTCUSDT | 0–50,000 | 0.40% | Not affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d) |
| 2023-12-24 09:35 | ETHUSDT | 0–50,000 | 0.40% | Not affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d) |
| 2023-12-24 09:35 | SOLUSDT | 0–10,000 | 0.50% | Not affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d) |
| 2024-01-02 12:50 | BNBUSDT | 0–10,000 | 0.50% | Not affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/d75c5ca94f704e96a6a4e55ffddfd65d) |
| 2024-05-28 10:30 | BTCUSDT | 0–50,000 | 0.40% | Not affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/aa735cd2d8bd4e7bb092179cf086e486) |
| 2025-06-17 06:30 | BTCUSDT | 0–300,000 | 0.40% | Affected | [Binance notice](https://www.binance.com/en-PH/support/announcement/detail/e778988edec446038ad536bcb0c8d460) |
| 2025-06-20 06:30 | XRPUSDT | 0–10,000 | 0.65% | Affected | [Binance notice](https://www.binance.com/en-PH/support/announcement/detail/e778988edec446038ad536bcb0c8d460) |
| 2025-06-20 06:30 | DOGEUSDT | 0–10,000 | 0.65% | Affected | [Binance notice](https://www.binance.com/en-PH/support/announcement/detail/e778988edec446038ad536bcb0c8d460) |
| 2025-08-19 06:30 | BTCUSDT | 0–300,000 | 0.40% | Affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/8e428625ebeb4cc7ae8678026846095c) |
| 2025-08-19 06:30 | ETHUSDT | 0–300,000 | 0.40% | Affected | [Binance notice](https://www.binance.com/en/support/announcement/detail/8e428625ebeb4cc7ae8678026846095c) |

For each listed first band, the initial tier has notional floor 0 and
maintenance amount/cumulative deduction `cum = 0`. Higher-tier `cum`
values must be calculated from the full adjacent tier schedule and reviewed
against Binance's published liquidation formula; do not copy an assumed
constant `cum` across bands.

The 2024-01-02 notice contains the before/after tables for BTCUSDT, ETHUSDT,
SOLUSDT and BNBUSDT. For example, the new BTCUSDT first band was 0–50,000
USDT at 0.40%; the new SOLUSDT and BNBUSDT first bands were 0–10,000 USDT at
0.50%. The notice explicitly says positions opened before the update were not
affected. The 2025-06-14 notice sets BTCUSDT at 2025-06-17 06:30 UTC and
XRPUSDT/DOGEUSDT at 2025-06-20 06:30 UTC; it explicitly says existing
positions are affected. The 2025-08-17 notice changes BTCUSDT and ETHUSDT at
2025-08-19 06:30 UTC and also explicitly says existing positions are affected.

This extraction still does not establish every target symbol's complete
historical table from November 2023 through April 2026. It also does not prove
that a symbol-month without an announcement had no tier change. The 240-row
ledger therefore remains authoritative for review state: 20 confirmed
announcement entries and 220 NOT_REVIEWED entries.

## Synthetic position sizing — schema v3

The pipeline no longer asks for fictitious historical account snapshots.
Schema v3 stores the synthetic research account once and derives a quantity
and isolated wallet balance independently for every event:

- Initial equity: 10,000 USDT.
- Risk budget: 0.5% of equity = 50 USDT per event.
- Maximum notional leverage: 3x.
- Margin mode: isolated; position mode: one-way; contract: USDT-linear.
- Quantity is the smaller of risk-budget sizing (including adverse entry/stop
  slippage and both-side fees) and the 3x notional cap.
- Initial margin = position notional / 3.
- Isolated wallet balance = initial margin less the entry-fee reserve.
- Each event resets to the same equity; no concurrent positions, compounding,
  portfolio allocation, or user account history is represented.

This is a reproducible single-event research assumption, not an actual account
snapshot. The manifest must still include complete time-bounded Binance tier
history with official source artifacts, hashes, and reviewer metadata. The
loader rejects gaps, overlaps, unbound files, unsupported contract scope, and
old schema versions.

## Funding archive review workflow

Run the read-only Funding audit against the already collected Drive files:

~~~powershell
python .\\scripts\\vike_v31_funding_archive_audit.py --drive-root "G:\\Mon Drive\\ai trade\\Vike V3.1"
~~~

The script produces a 240-file SHA-256/structure evidence packet and a
PENDING_REVIEW certificate template outside raw-staging. It checks CSV schema,
timestamps, duplicates, ordering, ranges, and observed settlement intervals.
It cannot prove that every settlement was collected.

Binance's USDⓈ-M API documents GET /fapi/v1/fundingRate as ascending history
with inclusive start/end milliseconds and a maximum limit of 1,000 records;
the response includes fundingTime, fundingRate and the associated markPrice.
An independently reviewed completeness claim must retain a paginated
retrieval log (symbol, time bounds, page boundaries, response counts and raw
response hashes), account for symbol-specific interval changes, and match all
240 current local file hashes. The draft must remain PENDING_REVIEW until
that work is complete. See the
[official USDⓈ-M Funding Rate History API](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History).

## Labeling remains blocked

No archive inventory can supply historical user-specific position quantity or
isolated wallet balance. Nor does a public maintenance-tier notice prove a
particular historical account state. Until the source artifacts and independent
position assumptions are reviewed and cryptographically bound, the pipeline
must remain fail-closed:

- Funding certificate: `NOT_VERIFIED` until semantic coverage evidence and
  each of the 240 Funding-file hashes are bound and reviewed.
- Liquidation evidence: `NOT_VERIFIED` until historical tier schedules and
  independent position assumptions have full interval coverage and verifiable
  evidence artifacts.
- Labels, walk-forward training and model approval: blocked.

The May–October 2026 holdout remains locked and is not part of this archive.
No raw-staging files are changed by this document.
