#!/usr/bin/env python3
"""Read-only Binance USD-M historical coverage probe.

Checks public exchange metadata and the first/most-recent available observations
for 15m trade klines, funding history, and mark-price klines. It does not
download a full three-year dataset, place orders, or require API credentials.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.historical_data_validation import normalize_epoch_ms


BASE_URL = "https://fapi.binance.com"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,ADAUSDT"
INTERVAL_MS = 15 * 60 * 1000
DATASETS = {
    "trade_15m": "/fapi/v1/klines",
    "funding": "/fapi/v1/fundingRate",
    "mark_15m": "/fapi/v1/markPriceKlines",
}


def _three_years_before(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year - 3)
    except ValueError:  # February 29
        return value.replace(year=value.year - 3, day=28)


def _session() -> requests.Session:
    retry = Retry(
        total=4, connect=4, read=4, backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4))
    session.headers.update({"User-Agent": "ai-crypto-trader-historical-coverage-probe/1.0"})
    return session


def _get(session, path, params=None):
    response = session.get(BASE_URL + path, params=params or {}, timeout=(10, 30))
    if response.status_code == 418:
        raise RuntimeError("Binance temporary IP ban (HTTP 418); stop probing and retry later")
    if response.status_code == 429:
        raise RuntimeError("Binance rate limit (HTTP 429); stop probing and retry later")
    response.raise_for_status()
    return response.json()


def _row_timestamp(row, *, now_ms, field):
    if isinstance(row, dict):
        value = row.get("fundingTime")
    elif isinstance(row, (list, tuple)) and row:
        value = row[0]
    else:
        value = None
    return normalize_epoch_ms(value, now_ms=now_ms, field=field)


def _probe_dataset(session, endpoint, symbol, start_ms, end_ms, now_ms):
    if endpoint == "funding":
        # Funding is sparse (typically every 8h); use a wider boundary window.
        first_end = min(end_ms, start_ms + 90 * 86_400_000)
        last_start = max(start_ms, end_ms - 90 * 86_400_000)
        first_params = {"symbol": symbol, "startTime": start_ms, "endTime": first_end, "limit": 1}
        last_params = {"symbol": symbol, "startTime": last_start, "endTime": end_ms, "limit": 1000}
    else:
        first_end = min(end_ms, start_ms + 7 * 86_400_000)
        last_start = max(start_ms, end_ms - 7 * 86_400_000)
        first_params = {
            "symbol": symbol, "interval": "15m",
            "startTime": start_ms, "endTime": first_end, "limit": 1,
        }
        last_params = {
            "symbol": symbol, "interval": "15m",
            "startTime": last_start, "endTime": end_ms, "limit": 1500,
        }

    first_rows = _get(session, endpoint, first_params)
    last_rows = _get(session, endpoint, last_params)
    if not isinstance(first_rows, list) or not isinstance(last_rows, list):
        raise RuntimeError("Unexpected non-list response")
    first_ts = _row_timestamp(first_rows[0], now_ms=now_ms, field=f"{symbol}.{endpoint}.first") if first_rows else None
    last_ts = _row_timestamp(last_rows[-1], now_ms=now_ms, field=f"{symbol}.{endpoint}.last") if last_rows else None
    expected_latest = end_ms - INTERVAL_MS
    tolerance = INTERVAL_MS * 2 if endpoint != "funding" else 24 * 60 * 60 * 1000
    earliest_ok = first_ts is not None and first_ts <= start_ms + tolerance
    latest_ok = last_ts is not None and last_ts >= expected_latest - tolerance
    return {
        "first_timestamp_ms": first_ts,
        "last_timestamp_ms": last_ts,
        "first_rows_found": len(first_rows),
        "recent_rows_found": len(last_rows),
        "earliest_boundary_ok": earliest_ok,
        "latest_boundary_ok": latest_ok,
        "boundary_coverage_ok": earliest_ok and latest_ok,
        "note": (
            "Boundary sampling only; this is not a full gap, checksum, or three-year continuity audit."
        ),
    }


def run_probe(symbols, start_ms, end_ms, output):
    now_ms = int(time.time() * 1000)
    session = _session()
    report = {
        "schema_version": 1,
        "provider": "Binance USD-M public REST",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_window": {
            "start_utc": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
            "end_utc_exclusive": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
            "duration_days": round((end_ms - start_ms) / 86_400_000, 3),
        },
        "symbols": {},
        "summary": {"pass": 0, "fail": 0, "inconclusive": 0, "blocked": 0},
        "status": "INCOMPLETE",
        "limitations": [
            "This is a low-volume boundary probe, not a complete historical download or gap audit.",
            "Current exchangeInfo cannot establish historical listing/delisting or survivorship-free universe membership.",
            "Passing boundary checks does not establish continuous funding/mark-price coverage.",
            "No credentials, private endpoints, orders, or paid services are used.",
        ],
    }
    try:
        try:
            exchange_info = _get(session, "/fapi/v1/exchangeInfo")
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 451:
                # HTTP 451 is a provider-side legal/geographic restriction.
                # Do not retry through alternate hosts or proxy services.
                report["status"] = "BLOCKED_GEO_RESTRICTION"
                report["summary"]["blocked"] = 1
                report["error"] = (
                    "Binance USD-M public API returned HTTP 451 for exchangeInfo. "
                    "The provider denied access due to a legal/geographic restriction; "
                    "no market-data coverage was established."
                )
                output_path = Path(output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(report["summary"], sort_keys=True))
                print("Probe blocked by Binance HTTP 451; report: " + str(output_path))
                return 0
            raise
        contracts = {
            row["symbol"]: row for row in exchange_info.get("symbols", [])
            if row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
        }
        for symbol in symbols:
            symbol_report = {
                "current_contract": None,
                "datasets": {},
                "status": "INCONCLUSIVE",
                "reasons": [],
            }
            contract = contracts.get(symbol)
            if not contract or contract.get("status") != "TRADING":
                symbol_report["reasons"].append("Not a currently trading USDT perpetual in exchangeInfo")
                report["symbols"][symbol] = symbol_report
                report["summary"]["inconclusive"] += 1
                continue
            symbol_report["current_contract"] = {
                "contract_type": contract.get("contractType"),
                "status": contract.get("status"),
                "base_asset": contract.get("baseAsset"),
                "quote_asset": contract.get("quoteAsset"),
                "onboard_date_ms": contract.get("onboardDate"),
            }
            for name, endpoint in DATASETS.items():
                try:
                    symbol_report["datasets"][name] = _probe_dataset(
                        session, endpoint, symbol, start_ms, end_ms, now_ms
                    )
                except Exception as exc:
                    symbol_report["datasets"][name] = {
                        "boundary_coverage_ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            failed = [
                name for name, item in symbol_report["datasets"].items()
                if not item.get("boundary_coverage_ok", False)
            ]
            if failed:
                symbol_report["status"] = "FAIL"
                symbol_report["reasons"].append("Missing/insufficient boundary data: " + ", ".join(failed))
                report["summary"]["fail"] += 1
            else:
                symbol_report["status"] = "PASS_BOUNDARIES_ONLY"
                report["summary"]["pass"] += 1
            report["symbols"][symbol] = symbol_report
            time.sleep(0.12)
    finally:
        session.close()

    report["status"] = "COMPLETED"
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], sort_keys=True))
    print(f"Report: {output_path}")
    return 1 if report["summary"]["fail"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--start", help="UTC ISO date/time; default: three calendar years ago")
    parser.add_argument("--end", help="UTC ISO date/time; default: now")
    parser.add_argument("--out", default="artifacts/binance-usdm-coverage.json")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start else _three_years_before(now)
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else now
    if start.tzinfo is None or end.tzinfo is None:
        parser.error("--start and --end must include a timezone")
    start_ms = int(start.astimezone(timezone.utc).timestamp() * 1000)
    end_ms = int(end.astimezone(timezone.utc).timestamp() * 1000)
    if start_ms >= end_ms:
        parser.error("--start must be earlier than --end")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols:
        parser.error("at least one symbol is required")
    return run_probe(symbols, start_ms, end_ms, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
