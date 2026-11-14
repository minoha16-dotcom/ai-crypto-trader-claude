"""Focused public-data smoke check for Bybit Spot and Futures.

Run from the repository root with:
    python scripts/check_bybit.py

No credentials, private endpoints, or orders are used.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.config import settings
from backend.exchanges import ExchangeRateLimitCooldown, ExchangeRegistry
from check_exchanges import check, is_region_restriction


async def main():
    registry = ExchangeRegistry()
    pause_seconds = max(0.0, float(os.getenv("SMOKE_REQUEST_PAUSE_SECONDS", "15")))
    passed = 0
    inconclusive = 0
    failed = 0
    cooldown = False
    try:
        print(f"BYBIT_API_HOSTNAME={settings.bybit_api_hostname}")
        for index, market_type in enumerate(("spot", "futures")):
            if cooldown:
                inconclusive += 1
                print(
                    f"INCONCLUSIVE bybit {market_type}: not retried after the "
                    "other market type activated the shared exchange/IP cooldown."
                )
                continue
            if index and pause_seconds:
                await asyncio.sleep(pause_seconds)
            try:
                result = await check(registry, "bybit", market_type)
            except Exception as error:
                if is_region_restriction(error):
                    inconclusive += 1
                    print(
                        f"INCONCLUSIVE bybit {market_type}: provider blocked "
                        "this runner region; this is not a successful check."
                    )
                elif isinstance(error, ExchangeRateLimitCooldown):
                    inconclusive += 1
                    cooldown = True
                    print(
                        f"INCONCLUSIVE bybit {market_type}: HTTP {error.status_code}; "
                        f"cooldown={error.retry_after}s."
                    )
                else:
                    failed += 1
                    print(f"FAIL bybit {market_type}: {type(error).__name__}: {error}")
            else:
                passed += 1
                suffix = (
                    f" contract={result['contract_type']} size={result['contract_size']} "
                    f"amount_precision={result['amount_precision']}"
                    if market_type == "futures" else
                    f" amount_precision={result['amount_precision']}"
                )
                print(
                    f"PASS bybit {market_type} {result['symbol']} "
                    f"last={result['last']}{suffix}"
                )
    finally:
        await registry.close()

    print(
        f"SUMMARY bybit passed={passed} inconclusive={inconclusive} failed={failed}"
    )
    if failed:
        return 1
    if inconclusive or passed != 2:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
