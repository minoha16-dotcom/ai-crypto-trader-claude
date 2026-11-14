"""Live smoke checks using the application's shared public-market registry.

Run: python scripts/check_exchanges.py
No API keys, private account endpoints, or orders are used.
"""
import asyncio
import math
import os
import sys
from pathlib import Path

# Running "python scripts/check_exchanges.py" puts scripts/, not always the
# repository root, at sys.path[0] (notably on GitHub-hosted runners).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.exchanges import ExchangeRegistry, ExchangeRateLimitCooldown

EXCHANGES = ("kucoin", "bybit", "okx")


async def check(registry, exchange_id, market_type):
    # Exercise the same market resolver, shared per-exchange CCXT throttler,
    # and ticker cache used by the FastAPI service.
    spec = await registry.market_spec(exchange_id, "BTC/USDT", market_type)
    symbol = spec["symbol"]
    ticker = await registry.ticker(exchange_id, symbol, market_type)

    last = ticker.get("last")
    if last is None or not math.isfinite(float(last)) or float(last) <= 0:
        raise RuntimeError("Ticker response did not contain a finite positive last price")

    contract_type = spec.get("contract_type")
    contract_size = spec.get("contract_size")
    amount_precision = spec.get("amount_precision")
    precision_mode = spec.get("precision_mode")

    if market_type == "futures":
        if contract_type not in ("linear", "inverse"):
            raise RuntimeError("Futures contract must be explicitly linear or inverse")
    elif contract_type != "spot":
        raise RuntimeError("Spot market metadata must identify a Spot instrument")

    if contract_size is None or not math.isfinite(float(contract_size)) or float(contract_size) <= 0:
        raise RuntimeError("Market has invalid contractSize")
    if amount_precision is None:
        raise RuntimeError("Market does not publish amount precision")
    try:
        precision = float(amount_precision)
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("Market amount precision is not numeric") from None
    if not math.isfinite(precision) or precision < 0:
        raise RuntimeError("Market amount precision must be finite and non-negative")
    if precision_mode == 2:
        if not precision.is_integer():
            raise RuntimeError("Decimal-place precision must be an integer")
    elif precision_mode == 3:
        if not precision.is_integer() or precision <= 0:
            raise RuntimeError("Significant-digit precision must be a positive integer")
    elif precision_mode == 4:
        if precision <= 0:
            raise RuntimeError("Tick-size precision must be positive")
    else:
        raise RuntimeError(f"Unsupported CCXT precision mode: {precision_mode}")

    for field in ("min_amount", "max_amount", "min_cost", "max_cost"):
        limit_value = spec.get(field)
        if limit_value is not None:
            try:
                numeric_limit = float(limit_value)
            except (TypeError, ValueError, OverflowError):
                raise RuntimeError(f"Market {field} is not numeric") from None
            if not math.isfinite(numeric_limit) or numeric_limit < 0:
                raise RuntimeError(f"Market {field} must be finite and non-negative")

    raw_timestamp = ticker.get("timestamp")
    if raw_timestamp is not None:
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError("Ticker timestamp is not numeric") from None
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise RuntimeError("Ticker timestamp must be finite and positive")

    return {
        "exchange": exchange_id,
        "market_type": market_type,
        "symbol": symbol,
        "last": float(last),
        "ticker_timestamp": raw_timestamp,
        "contract_type": contract_type,
        "contract_size": float(contract_size),
        "amount_precision": precision,
        "precision_mode": precision_mode,
        "min_amount": spec.get("min_amount"),
        "max_amount": spec.get("max_amount"),
        "min_cost": spec.get("min_cost"),
        "max_cost": spec.get("max_cost"),
    }


def is_region_restriction(error):
    """Identify explicit exchange/provider geo-block responses, not generic failures."""
    message = str(error).lower()
    return (
        "restricted location" in message
        or "configured to block access from your country" in message
        or "http 451" in message
    )


async def main():
    registry = ExchangeRegistry()
    pause_seconds = max(0.0, float(os.getenv("SMOKE_REQUEST_PAUSE_SECONDS", "5.0")))
    failed = 0
    passed = 0
    skipped = 0
    cooldown_blocked = 0
    inconclusive = 0
    cooldown_exchanges = set()
    checks = [
        (exchange_id, market_type)
        for exchange_id in EXCHANGES
        for market_type in ("spot", "futures")
    ]
    try:
        for index, (exchange_id, market_type) in enumerate(checks):
            if exchange_id in cooldown_exchanges:
                inconclusive += 1
                print(
                    f"INCONCLUSIVE {exchange_id} {market_type}: skipped after "
                    "the other market type triggered a rate-limit cooldown."
                )
                continue
            if index and pause_seconds:
                await asyncio.sleep(pause_seconds)
            try:
                result = await check(registry, exchange_id, market_type)
            except Exception as error:
                if is_region_restriction(error):
                    skipped += 1
                    print(
                        f"SKIP {exchange_id} {market_type}: provider blocked this runner region; "
                        "this is not a successful connectivity check."
                    )
                elif isinstance(error, ExchangeRateLimitCooldown):
                    cooldown_blocked += 1
                    inconclusive += 1
                    print(
                        f"COOLDOWN {exchange_id} {market_type}: HTTP {error.status_code}; "
                        f"local cooldown applied for {error.retry_after}s."
                    )
                    # Do not immediately re-hit the same venue through its other
                    # market client. Bybit applies shared request/IP protections;
                    # the registry will enforce the cooldown for normal app traffic.
                    cooldown_exchanges.add(exchange_id)
                else:
                    failed += 1
                    print(f"FAIL {exchange_id} {market_type}: {type(error).__name__}: {error}")
            else:
                passed += 1
                suffix = (
                    f" contract={result['contract_type']} size={result['contract_size']} "
                    f"amount_precision={result['amount_precision']}"
                    if market_type == "futures" else
                    f" amount_precision={result['amount_precision']}"
                )
                print(
                    f"PASS {exchange_id} {market_type} {result['symbol']} "
                    f"last={result['last']}{suffix}"
                )
    finally:
        await registry.close()

    # Geo blocks and HTTP 429s are environmental limitations, not PASS and not code failures.
    print(
        f"SUMMARY passed={passed} region_skipped={skipped} "
        f"cooldown_blocked={cooldown_blocked} failed={failed}"
    )
    if skipped or inconclusive:
        print(f"INCONCLUSIVE: {skipped + inconclusive} checks could not be verified from this runner.")
    if passed == 0 and failed == 0:
        print("INCONCLUSIVE: no exchange endpoint could be verified from this runner region.")
    if failed:
        return 1
    if skipped or inconclusive:
        # A green workflow must mean all six public market-data checks ran.
        # Provider geo-blocks and cooldowns are neither code failures nor passes.
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
