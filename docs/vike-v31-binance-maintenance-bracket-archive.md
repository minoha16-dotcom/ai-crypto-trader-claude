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
| 2024-08-22 09:30 | SOLUSDT, DOGEUSDT | Not affected | [Binance 2024-08-22 announcement](https://www.binance.com/en/support/announcement/detail/572598a13c384de6aa3c4d4dc1be5f81) |
| 2024-08-27 07:30 | XRPUSDT, BNBUSDT | Not affected | [Binance 2024-08-27 announcement](https://www.binance.com/fr/support/announcement/detail/194e244a94e9416cb400318dbed2f4b9) |
| 2024-09-17 07:30 | LINKUSDT | Not affected | [Binance 2024-09-17 announcement](https://www.binance.com/en/support/announcement/detail/a05ae5aad6e44f6c848bde0a4c94ad4b) |
| 2024-11-07 | XRPUSDT | Not affected | [Binance announcement archive result](https://www.binance.com/en/support/announcement/detail/) — exact announcement URL must be re-verified before importing tier rows |
| 2024-11-13 | DOGEUSDT | Not affected | [Binance announcement archive result](https://www.binance.com/en/support/announcement/detail/) — exact announcement URL must be re-verified before importing tier rows |
| 2024-11-19 | ADAUSDT | Not affected | [Binance announcement archive result](https://www.binance.com/en/support/announcement/detail/) — exact announcement URL must be re-verified before importing tier rows |
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
- 2025-11-28, 2025-12-05 and 2025-12-12 results located other contracts.
- 2026-01-12, 2026-01-30, 2026-02-03, 2026-02-27, 2026-03-07,
  2026-03-10, 2026-03-20 and 2026-04-10 results were checked. The surfaced
  tier updates named other contracts, not the eight target symbols. The
  2026-04-10 notice named XPLUSDT, FFUSDT, 0GUSDT and WIFUSDT.

Portfolio Margin collateral-ratio changes are not USD-M maintenance-bracket
changes. COIN-M, USDC-M, BUSD contracts, isolated spot margin and quarterly
delivery contracts are outside this archive.

## Important implementation consequence

A single current bracket table per symbol is not sufficient. The research
engine must preserve, at minimum:

1. **Maintenance bracket history** — symbol, effective UTC timestamp, prior
   and new tier tables, notional floors/caps, maintenance-margin rate, and
   maintenance amount / cumulative deduction (`cum`) where published.
2. **Applicability to existing positions** — explicit
   `AFFECTED`, `NOT_AFFECTED`, or `NOT_STATED` copied from the notice.
3. **Position assumptions** — independently time-bounded quantity, isolated
   wallet balance, margin mode, position mode, contract type and external-flow
   assumptions. These are not supplied by tier announcements and must not be
   fabricated from them.
4. **Evidence binding** — source URL, retrieval time, exact downloaded source
   artifact, SHA-256 of that artifact, reviewer and review timestamp. A
   64-character hash-shaped string without a source artifact is not evidence.
5. **Coverage ledger** — one explicit status for each symbol × month:
   `CONFIRMED_CHANGE`, `SEARCHED_NO_MATCH`, or `NOT_REVIEWED`. Only the
   first two can contribute to a reviewed completeness claim, and
   `SEARCHED_NO_MATCH` must retain the search method and evidence.

The existing PR #55 schema v1 does not yet meet this evidence-binding standard:
it validates the syntax of `evidence_sha256` and `snapshot_sha256` but does
not prove that either hash matches an accessible source artifact. Its
`positions[].brackets` also combines two distinct concepts: exchange tier
history and assumed account/position state. Do not create a VERIFIED v1
manifest from this inventory.

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
