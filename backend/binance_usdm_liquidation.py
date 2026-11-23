"""Independent Binance USD-M isolated one-way liquidation-price verifier.

This is a research verifier, not Binance's risk engine. Cross margin, hedge mode,
multi-assets collateral, multi-position portfolio effects and bracket changes
during the holding period are deliberately unsupported and fail closed.
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

def _finite_positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)

def validate_brackets(brackets: Iterable[MaintenanceBracket]) -> list[MaintenanceBracket]:
    rows=sorted(list(brackets),key=lambda x:x.notional_floor)
    if not rows: raise ValueError("maintenance brackets are required")
    previous_cap=0.0
    for i,b in enumerate(rows):
        floor=_finite_positive(b.notional_floor,"notional_floor") if b.notional_floor else 0.0
        cap=_finite_positive(b.notional_cap,"notional_cap")
        mmr=b.maint_margin_ratio
        cum=b.cum
        if isinstance(mmr,bool) or not isinstance(mmr,(int,float)) or not math.isfinite(float(mmr)) or not 0 < mmr < 1:
            raise ValueError("maint_margin_ratio must be finite and in (0,1)")
        if isinstance(cum,bool) or not isinstance(cum,(int,float)) or not math.isfinite(float(cum)) or cum < 0:
            raise ValueError("cum must be finite and nonnegative")
        if i==0 and floor!=0: raise ValueError("first bracket must start at zero")
        if i and not math.isclose(floor,previous_cap,rel_tol=0,abs_tol=1e-8):
            raise ValueError("maintenance brackets must be contiguous and non-overlapping")
        if cap<=floor: raise ValueError("bracket cap must exceed floor")
        previous_cap=cap
    return rows

def derive_brackets_from_published_tiers(tier_rows: Iterable[dict]) -> list[MaintenanceBracket]:
    """Derive Binance cumulative maintenance deductions from published floor/cap/MMR rows.

    Binance announcements publish notional bands and MMR. The cumulative
    maintenance amount is reconstructed recursively to keep maintenance
    margin continuous at each tier boundary:
    cum[i] = cum[i-1] + floor[i] * (mmr[i] - mmr[i-1]).
    The resulting schedule is still subject to source review and hash binding.
    """
    rows = sorted(list(tier_rows), key=lambda row: float(row["notional_floor"]))
    if not rows:
        raise ValueError("published maintenance tiers are required")
    brackets = []
    previous_cap = None
    previous_mmr = None
    previous_cum = 0.0
    for index, row in enumerate(rows):
        try:
            floor = float(row["notional_floor"])
            cap = float(row["notional_cap"])
            mmr = float(row["maint_margin_ratio"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("published tier requires numeric floor, cap and MMR") from exc
        if not all(math.isfinite(value) for value in (floor, cap, mmr)):
            raise ValueError("published tier values must be finite")
        if floor < 0 or cap <= floor or not 0 < mmr < 1:
            raise ValueError("published tier floor/cap/MMR is invalid")
        if index == 0:
            if floor != 0:
                raise ValueError("first published tier must start at zero")
            cum = 0.0
        else:
            if not math.isclose(floor, previous_cap, rel_tol=0, abs_tol=1e-8):
                raise ValueError("published maintenance tiers must be contiguous")
            cum = previous_cum + floor * (mmr - previous_mmr)
        brackets.append(MaintenanceBracket(floor, cap, mmr, cum))
        previous_cap, previous_mmr, previous_cum = cap, mmr, cum
    return validate_brackets(brackets)


def calculate_isolated_liquidation_price(*, side: str, entry_price: float,
        quantity: float, isolated_wallet_balance: float,
        brackets: Iterable[MaintenanceBracket], margin_mode: str="ISOLATED",
        position_mode: str="ONE_WAY", contract_type: str="USDT_LINEAR") -> dict:
    """Solve Binance USD-M isolated one-way formula against bracketed MMR/cum.

    Formula follows Binance USDⓈ-M FAQ: LP=(WB+cum-side*q*entry)/(q*MMR-side*q),
    where side=+1 LONG/-1 SHORT, WB is isolatedWalletBalance.
    The candidate is accepted only if its notional lies in that same bracket.
    """
    if margin_mode!="ISOLATED" or position_mode!="ONE_WAY" or contract_type!="USDT_LINEAR":
        raise ValueError("unsupported liquidation scope; only isolated one-way USDT-linear is supported")
    if side not in ("LONG","SHORT"): raise ValueError("side must be LONG or SHORT")
    entry=_finite_positive(entry_price,"entry_price")
    qty=_finite_positive(quantity,"quantity")
    wallet=_finite_positive(isolated_wallet_balance,"isolated_wallet_balance")
    tiers=validate_brackets(brackets)
    direction=1.0 if side=="LONG" else -1.0
    candidates=[]
    for tier in tiers:
        denominator=qty*(tier.maint_margin_ratio-direction)
        if math.isclose(denominator,0.0,abs_tol=1e-15): continue
        lp=(wallet+tier.cum-direction*qty*entry)/denominator
        if not math.isfinite(lp) or lp<=0: continue
        notional=lp*qty
        if tier.notional_floor <= notional < tier.notional_cap:
            candidates.append((lp,tier,notional))
    if len(candidates)!=1:
        raise ValueError(f"liquidation price has {len(candidates)} self-consistent maintenance-bracket solutions")
    lp,tier,notional=candidates[0]
    return {"status":"PASS","liquidation_price":lp,"side":side,
            "position_notional_at_liquidation":notional,
            "maintenance_margin_rate":tier.maint_margin_ratio,
            "maintenance_amount":tier.cum,"bracket_floor":tier.notional_floor,
            "bracket_cap":tier.notional_cap,"margin_mode":"ISOLATED",
            "position_mode":"ONE_WAY","contract_type":"USDT_LINEAR",
            "formula":"(isolatedWalletBalance + cum - side*qty*entryPrice) / (qty*MMR - side*qty)",
            "scope":"isolated_one_way_single_position_only",
            "limitations":["No cross-margin or portfolio offsets","No hedge-mode positions",
            "No multi-assets collateral haircuts","No dynamic bracket changes after entry",
            "No liquidation clearance fee or exchange risk-engine microstructure model"]}
