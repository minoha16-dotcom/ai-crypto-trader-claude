"""Binance USD-M cross/isolated, one-way/hedge liquidation verifier.

Research-only implementation of Binance's published algebraic liquidation
formula. It is not Binance's risk engine and requires point-in-time bracket
and account-state inputs. No API calls or trading actions are performed.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class MaintenanceBracket:
    notional_floor: float
    notional_cap: float
    maint_margin_ratio: float
    cum: float


def _num(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def validate_brackets(brackets: Iterable[MaintenanceBracket]):
    rows = sorted(list(brackets), key=lambda b: b.notional_floor)
    if not rows:
        raise ValueError("at least one dated maintenance bracket is required")
    prior = 0.0
    for i, b in enumerate(rows):
        floor = _num(b.notional_floor, "notional_floor", nonnegative=True)
        cap = _num(b.notional_cap, "notional_cap", positive=True)
        mmr = _num(b.maint_margin_ratio, "maint_margin_ratio", positive=True)
        _num(b.cum, "cum", nonnegative=True)
        if not 0 < mmr < 1:
            raise ValueError("maint_margin_ratio must be in (0, 1)")
        if i == 0 and floor != 0:
            raise ValueError("first bracket must start at zero")
        if i and not math.isclose(floor, prior, rel_tol=0, abs_tol=1e-8):
            raise ValueError("brackets must be contiguous and non-overlapping")
        if cap <= floor:
            raise ValueError("bracket cap must exceed floor")
        prior = cap
    return rows


def _tier_for_notional(brackets, notional):
    matches = [b for b in brackets if b.notional_floor <= notional < b.notional_cap]
    return matches[0] if len(matches) == 1 else None


def _solve_target(*, wallet, tmm_other, upnl_other, long_qty, long_entry,
                  short_qty, short_entry, long_brackets, short_brackets,
                  margin_mode, position_mode):
    lb = validate_brackets(long_brackets) if long_qty else []
    sb = validate_brackets(short_brackets) if short_qty else []
    candidates = []
    long_tiers = lb if long_qty else [None]
    short_tiers = sb if short_qty else [None]
    for lt in long_tiers:
        for st in short_tiers:
            mm_long = lt.maint_margin_ratio if lt else 0.0
            mm_short = st.maint_margin_ratio if st else 0.0
            cum_long = lt.cum if lt else 0.0
            cum_short = st.cum if st else 0.0
            numerator = (
                wallet - tmm_other + upnl_other + cum_long + cum_short
                - long_qty * long_entry + short_qty * short_entry
            )
            denominator = (
                long_qty * mm_long + short_qty * mm_short
                - long_qty + short_qty
            )
            if math.isclose(denominator, 0.0, abs_tol=1e-15):
                continue
            price = numerator / denominator
            if not math.isfinite(price) or price <= 0:
                continue
            long_notional = price * long_qty
            short_notional = price * short_qty
            if long_qty and not (lt.notional_floor <= long_notional < lt.notional_cap):
                continue
            if short_qty and not (st.notional_floor <= short_notional < st.notional_cap):
                continue
            candidates.append((price, lt, st, long_notional, short_notional))
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one self-consistent bracket solution; found {len(candidates)}"
        )
    price, lt, st, ln, sn = candidates[0]
    return {
        "status": "PASS",
        "liquidation_price": price,
        "margin_mode": margin_mode,
        "position_mode": position_mode,
        "contract_type": "USDT_LINEAR",
        "long_bracket": None if lt is None else {
            "floor": lt.notional_floor, "cap": lt.notional_cap,
            "mmr": lt.maint_margin_ratio, "cum": lt.cum,
        },
        "short_bracket": None if st is None else {
            "floor": st.notional_floor, "cap": st.notional_cap,
            "mmr": st.maint_margin_ratio, "cum": st.cum,
        },
        "long_notional_at_liquidation": ln,
        "short_notional_at_liquidation": sn,
        "scope": "single_symbol_usdt_linear_position_with_supplied_account_state",
        "limitations": [
            "Requires exact point-in-time wallet balance and other-position TMM/UPNL",
            "Requires historically effective bracket tiers for the liquidation timestamp",
            "Does not model multi-assets collateral haircuts or portfolio risk-engine adjustments",
            "Does not model liquidation execution slippage, fees, insurance fund or ADL",
            "Not valid for COIN-M, portfolio margin, or unrecorded collateral/account changes",
        ],
    }


def calculate_binance_usdm_liquidation_price(
    *, margin_mode: str, position_mode: str,
    wallet_balance: float, long_qty: float = 0.0, long_entry_price: float = 0.0,
    short_qty: float = 0.0, short_entry_price: float = 0.0,
    long_brackets: Iterable[MaintenanceBracket] = (),
    short_brackets: Iterable[MaintenanceBracket] = (),
    other_maintenance_margin: float = 0.0,
    other_unrealized_pnl: float = 0.0,
    contract_type: str = "USDT_LINEAR",
):
    """Calculate the common liquidation threshold for one-way or hedge positions.

    For isolated mode, wallet_balance must be the isolated wallet allocated to
    this position and both other_* values must be zero. For cross mode, wallet
    balance and other_* values must describe the same account snapshot.
    In one-way mode provide either long_qty or short_qty, not both. In hedge
    mode both legs may be nonzero and the returned price is the common
    algebraic threshold implied by Binance's published hedge-mode formula.
    """
    if margin_mode not in ("ISOLATED", "CROSSED"):
        raise ValueError("margin_mode must be ISOLATED or CROSSED")
    if position_mode not in ("ONE_WAY", "HEDGE"):
        raise ValueError("position_mode must be ONE_WAY or HEDGE")
    if contract_type != "USDT_LINEAR":
        raise ValueError("only USDT_LINEAR contracts are supported")
    wallet = _num(wallet_balance, "wallet_balance", positive=True)
    lq = _num(long_qty, "long_qty", nonnegative=True)
    sq = _num(short_qty, "short_qty", nonnegative=True)
    le = _num(long_entry_price, "long_entry_price", nonnegative=True)
    se = _num(short_entry_price, "short_entry_price", nonnegative=True)
    tmm = _num(other_maintenance_margin, "other_maintenance_margin", nonnegative=True)
    upnl = _num(other_unrealized_pnl, "other_unrealized_pnl")
    if not lq and not sq:
        raise ValueError("at least one nonzero position leg is required")
    if lq and not le or sq and not se:
        raise ValueError("entry price is required for each nonzero position leg")
    if position_mode == "ONE_WAY" and lq and sq:
        raise ValueError("ONE_WAY mode accepts only one directional leg")
    if margin_mode == "ISOLATED" and (tmm != 0 or upnl != 0):
        raise ValueError("isolated mode requires other TMM and UPNL to be zero")
    return _solve_target(
        wallet=wallet, tmm_other=tmm, upnl_other=upnl,
        long_qty=lq, long_entry=le, short_qty=sq, short_entry=se,
        long_brackets=long_brackets, short_brackets=short_brackets,
        margin_mode=margin_mode, position_mode=position_mode,
    )
