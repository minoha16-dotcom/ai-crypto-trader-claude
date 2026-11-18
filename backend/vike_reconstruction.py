"""Accounting-first reconstruction of Vike Hyperliquid wallet position lifecycles."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Iterable

EPS = 1e-9
USD_FEES = {"USD", "USDC"}

def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be finite") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result

def _ts(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("fill_ts must be timezone-aware")
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, bool) or value is None:
        raise ValueError("fill_ts is required")
    if isinstance(value, (int, float)):
        result = int(value)
        if result <= 0:
            raise ValueError("fill_ts must be positive")
        return result
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("fill_ts must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)

def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    wallet, asset = str(row.get("wallet") or "").strip().lower(), str(row.get("asset") or "").strip()
    side = str(row.get("side") or "").strip().lower()
    if not wallet or not asset or side not in {"buy", "sell"}:
        raise ValueError("wallet, asset and side=buy|sell are required")
    size, price = _num(row.get("size_token"), "size_token"), _num(row.get("price"), "price")
    before = _num(row.get("start_position"), "start_position")
    fee, pnl = _num(row.get("fee"), "fee"), _num(row.get("closed_pnl"), "closed_pnl")
    token, trade_id = str(row.get("fee_token") or "").strip().upper(), str(row.get("trade_id") or "")
    if size <= 0 or price <= 0 or not token or not trade_id:
        raise ValueError("size_token, price, fee_token and trade_id must be valid")
    return {"wallet":wallet,"asset":asset,"side":side,"size":size,"price":price,
            "before":before,"fee":fee,"fee_token":token,"pnl":pnl,
            "ts":_ts(row.get("fill_ts")),"trade_id":trade_id}

@dataclass
class _Position:
    wallet: str
    asset: str
    side: str
    qty: float
    entry_qty: float
    entry_price: float | None
    entry_ts: int | None
    carry_in: bool
    fees: float = 0.0
    pnl: float = 0.0
    fills: int = 0
    close_fills: int = 0
    issues: set[str] = field(default_factory=set)

def _side(qty: float) -> str:
    return "LONG" if qty > 0 else "SHORT"

def reconstruct_vike_fills(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild per-wallet, per-asset lifecycles; never promote carry-ins to complete."""
    parsed, errors, seen = [], Counter(), set()
    for raw in rows:
        try:
            row = _normalise(raw)
        except (ValueError, TypeError) as exc:
            errors[f"INVALID_FILL:{exc}"] += 1
            continue
        key = (row["wallet"], row["asset"], row["ts"], row["trade_id"])
        if key in seen:
            errors["DUPLICATE_WALLET_FILL"] += 1
            continue
        seen.add(key)
        parsed.append(row)
    parsed.sort(key=lambda r: (r["wallet"], r["asset"], r["ts"], r["trade_id"]))
    active, closed = {}, []

    def finish(p: _Position, row: dict[str, Any], status: str):
        closed.append({
            "wallet":p.wallet,"asset":p.asset,"side":p.side,
            "entry_ts":p.entry_ts,"exit_ts":row["ts"],
            "entry_price":p.entry_price,"exit_price":row["price"],
            "entry_quantity":p.entry_qty,"realized_pnl_usd":p.pnl,
            "fees_usd":p.fees,"net_realized_pnl_usd":p.pnl-p.fees,
            "fill_count":p.fills,"close_fill_count":p.close_fills,
            "exit_trade_id":row["trade_id"],"reconstruction_status":status,
            "issues":sorted(p.issues),
        })

    for r in parsed:
        key = (r["wallet"], r["asset"])
        delta = r["size"] if r["side"] == "buy" else -r["size"]
        before, after = r["before"], r["before"] + delta
        p = active.get(key)
        if p is None and abs(before) > EPS:
            p = _Position(r["wallet"],r["asset"],_side(before),abs(before),abs(before),
                          None,None,True,issues={"CARRY_IN_ENTRY_NOT_OBSERVED"})
            active[key] = p
        elif p is not None:
            expected = p.qty if p.side == "LONG" else -p.qty
            if abs(before-expected) > max(1e-8,abs(expected)*1e-6):
                p.issues.add("START_POSITION_MISMATCH")
        p = active.get(key)
        supported = r["fee_token"] in USD_FEES
        if abs(before) <= EPS and abs(after) > EPS:
            p = _Position(r["wallet"],r["asset"],_side(after),abs(after),r["size"],
                          r["price"],r["ts"],False, fills=1)
            if supported: p.fees += r["fee"]
            else: p.issues.add("UNSUPPORTED_FEE_CURRENCY")
            active[key] = p
            continue
        if p is None:
            errors["UNSEEDED_POSITION_STATE"] += 1
            continue
        p.fills += 1
        if not supported: p.issues.add("UNSUPPORTED_FEE_CURRENCY")
        if abs(after) <= EPS:
            p.pnl += r["pnl"]; p.close_fills += 1
            if supported: p.fees += r["fee"]
            status = "INCOMPLETE_CARRY_IN" if p.carry_in else ("INCOMPLETE_ACCOUNTING" if p.issues else "COMPLETE")
            finish(p,r,status); active.pop(key,None)
        elif before * after < 0:
            close_qty, open_qty = abs(before), abs(after)
            close_fee = r["fee"] * close_qty / r["size"]
            p.pnl += r["pnl"]; p.close_fills += 1
            if supported: p.fees += close_fee
            status = "INCOMPLETE_CARRY_IN" if p.carry_in else ("INCOMPLETE_ACCOUNTING" if p.issues else "COMPLETE")
            finish(p,r,status)
            new = _Position(r["wallet"],r["asset"],_side(after),open_qty,open_qty,
                            r["price"],r["ts"],False,fills=1)
            if supported: new.fees = r["fee"]-close_fee
            else: new.issues.add("UNSUPPORTED_FEE_CURRENCY")
            active[key] = new
        elif (before > 0 and after > 0) or (before < 0 and after < 0):
            if abs(after) > abs(before)+EPS:
                added = abs(after)-abs(before)
                if p.entry_price is not None:
                    p.entry_price = (p.entry_price*p.qty+r["price"]*added)/abs(after)
                    p.entry_qty += added
                p.qty = abs(after)
                if supported: p.fees += r["fee"]
            else:
                p.pnl += r["pnl"]; p.close_fills += 1; p.qty = abs(after)
                if supported: p.fees += r["fee"]
        else:
            p.issues.add("UNCLASSIFIED_POSITION_TRANSITION")

    opened = [{
        "wallet":p.wallet,"asset":p.asset,"side":p.side,
        "remaining_quantity":p.qty,"entry_quantity":p.entry_qty,
        "average_entry_price":p.entry_price,"entry_ts":p.entry_ts,
        "carry_in":p.carry_in,"realized_pnl_usd":p.pnl,"fees_usd":p.fees,
        "fill_count":p.fills,"issues":sorted(p.issues),"reconstruction_status":"OPEN_AT_END"
    } for p in active.values()]
    counts = Counter(x["reconstruction_status"] for x in closed)
    return {
        "schema_version":1,"source":"vike_hyperliquid_fills","fills_seen":len(parsed),
        "closed_positions":closed,"open_positions":opened,
        "diagnostics":{
            "invalid_or_skipped_rows":sum(errors.values()),"errors":dict(sorted(errors.items())),
            "closed_positions_by_status":dict(sorted(counts.items())),
            "complete_closed_positions":counts.get("COMPLETE",0),
            "incomplete_closed_positions":sum(v for k,v in counts.items() if k!="COMPLETE"),
            "open_positions_at_end":len(opened),
            "note":"Net realized PnL = sum(closed_pnl) - USD/USDC fees. Carry-in entries are never complete."
        }
    }
