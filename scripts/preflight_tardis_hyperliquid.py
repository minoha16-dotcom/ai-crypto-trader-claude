"""Preflight Tardis.dev Hyperliquid historical access before any bulk download.

Checks exchange metadata, API-key entitlement and one daily trades CSV per symbol.
Never prints or persists the API key. This is a validation step, not a bulk collector.
"""
import argparse
import csv
import gzip
import io
import json
import os
from datetime import date as date_type, datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE = "https://api.tardis.dev/v1"
DATASETS = "https://datasets.tardis.dev/v1"
EXCHANGE = "hyperliquid"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")


def get_json(url, api_key=None):
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def get_dataset_sample(symbol, date, api_key):
    year, month, day = date[:4], date[5:7], date[8:10]
    url = f"{DATASETS}/{EXCHANGE}/trades/{year}/{month}/{day}/{quote(symbol, safe='')}.csv.gz"
    headers = {"Accept": "application/gzip"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=90) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace").strip()
        if exc.code == 404:
            return {"status": "not_found", "http_status": 404, "rows": 0, "detail": detail}
        if exc.code in (400, 401, 403):
            status = "bad_request" if exc.code == 400 else "access_denied"
            return {
                "status": status,
                "http_status": exc.code,
                "rows": 0,
                "detail": detail,
                "reason": (
                    "Tardis rejected the dataset URL or one of its path parameters."
                    if exc.code == 400 else
                    "Tardis rejected this authenticated dataset request; check subscription entitlement and API-key access window."
                ),
            }
        raise
    try:
        decoded = gzip.decompress(payload).decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Invalid gzip CSV response for {symbol} on {date}") from exc
    reader = csv.DictReader(io.StringIO(decoded))
    fields = reader.fieldnames or []
    required = {"exchange", "symbol", "timestamp", "local_timestamp", "price", "amount"}
    if not required.issubset(set(fields)):
        raise RuntimeError(f"Unexpected Tardis trades CSV schema for {symbol}: {fields}")
    count = sum(1 for _ in reader)
    return {"status": "ok" if count else "empty", "rows": count, "url": url}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = p.parse_args()
    key = os.environ.get("TARDIS_API_KEY", "").strip()
    if not key:
        p.error("TARDIS_API_KEY is required. Add it as a GitHub Actions secret; never paste it into chat.")
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    exchange = get_json(f"{BASE}/exchanges/{EXCHANGE}")
    if not isinstance(exchange, dict):
        raise RuntimeError(f"Unexpected Tardis exchange metadata type: {type(exchange).__name__}")
    # Use the documented datasets.symbols allow-list, not optional boolean flags.
    datasets = exchange.get("datasets")
    if not isinstance(datasets, dict):
        raise RuntimeError(
            "Tardis exchange metadata has no datasets object. "
            f"Top-level fields returned: {sorted(exchange.keys())}"
        )
    dataset_symbols = datasets.get("symbols")
    if not isinstance(dataset_symbols, list):
        raise RuntimeError(
            "Tardis exchange metadata has no datasets.symbols list. "
            f"datasets fields returned: {sorted(datasets.keys())}"
        )
    metadata = {
        str(item.get("id", "")).upper(): item
        for item in dataset_symbols if isinstance(item, dict) and item.get("id")
    }
    if not metadata:
        raise RuntimeError(
            "Tardis metadata returned an empty datasets.symbols list. "
            f"Exchange fields: {sorted(exchange.keys())}; "
            f"datasets fields: {sorted(datasets.keys())}"
        )

    entitlement = get_json(f"{BASE}/api-key-info", key)
    # Tardis /api-key-info may return a list (not only an object).
    # Summarize its shape without logging the key or arbitrary response values.
    if isinstance(entitlement, dict):
        allowed = ("exchanges", "from", "to", "dataTypes", "data_types", "datasets")
        entitlement_summary = {
            name: entitlement.get(name)
            for name in allowed if name in entitlement
        }
        entitlement_summary["response_type"] = "object"
        entitlement_summary["fields"] = sorted(entitlement.keys())
    elif isinstance(entitlement, list):
        entitlement_summary = {
            "response_type": "list",
            "item_count": len(entitlement),
            "item_fields": sorted({
                key_name
                for item in entitlement if isinstance(item, dict)
                for key_name in item.keys()
            }),
        }
        # Include only recognizable non-secret entitlement dimensions.
        safe_fields = ("exchange", "exchanges", "from", "to", "dataType", "dataTypes",
                       "data_type", "data_types", "dataset")
        entitlement_summary["items"] = [
            {name: item[name] for name in safe_fields if name in item}
            for item in entitlement if isinstance(item, dict)
        ]
    else:
        raise RuntimeError(
            "Unexpected Tardis /api-key-info response type: "
            f"{type(entitlement).__name__}; expected object or list"
        )
    checks = []
    failures = []
    for symbol in symbols:
        item = metadata.get(symbol.upper())
        if not item:
            failures.append(f"{symbol}: not present in Tardis downloadable dataset symbols")
            checks.append({"symbol": symbol, "status": "symbol_unavailable"})
            continue
        available_since = item.get("availableSince") or datasets.get("exportedFrom") or exchange.get("availableSince")
        if not available_since:
            failures.append(f"{symbol}: Tardis did not publish an availableSince date")
            checks.append({"symbol": symbol, "status": "missing_available_since"})
            continue
        data_types = item.get("dataTypes") or []
        if "trades" not in data_types:
            failures.append(f"{symbol}: trades is not listed in this instrument's Tardis dataTypes")
            checks.append({
                "symbol": symbol,
                "dataset_symbol_id": item.get("id"),
                "available_since": available_since,
                "dataset_types": data_types,
                "status": "trades_unavailable",
            })
            continue

        # Public CSV samples are only guaranteed on month starts. If collection
        # began mid-month, that month's day-01 predates the instrument and can
        # produce HTTP 400. Select the first month-start on/after availableSince.
        available_date = date_type.fromisoformat(available_since[:10])
        sample_date = available_date.replace(day=1)
        if sample_date < available_date:
            year, month = sample_date.year, sample_date.month + 1
            if month == 13:
                year, month = year + 1, 1
            sample_date = date_type(year, month, 1)
        date = sample_date.isoformat()
        dataset_symbol_id = str(item.get("id"))
        sample = get_dataset_sample(dataset_symbol_id, date, "")
        ok = sample["status"] == "ok" and sample["rows"] > 0

        # A first-of-month sample is public and does not prove paid historical
        # entitlement. Probe the immediately following day with the API key.
        paid_probe_date = (available_date.replace(day=1) + __import__("datetime").timedelta(days=1))
        if paid_probe_date < available_date:
            paid_probe_date = available_date + __import__("datetime").timedelta(days=1)
        paid_probe_day = paid_probe_date.isoformat()
        paid_probe = get_dataset_sample(dataset_symbol_id, paid_probe_day, key)
        paid_access_ok = paid_probe["status"] == "ok" and paid_probe["rows"] > 0

        checks.append({
            "symbol": symbol,
            "available_since": available_since,
            "dataset_symbol_id": dataset_symbol_id,
            "dataset_types": data_types,
            "public_sample_access": "public_monthly_sample_no_api_key",
            "sample_date": date,
            "sample": sample,
            "paid_entitlement_probe_date": paid_probe_day,
            "paid_entitlement_probe": paid_probe,
            "paid_entitlement_probe_status": "pass" if paid_access_ok else "not_confirmed",
            "status": "pass" if ok else "sample_unavailable",
        })
        if not ok:
            failures.append(
                f"{symbol} ({dataset_symbol_id}): public trades sample on {date} "
                f"returned {sample['status']} ({sample['rows']} rows)"
            )
        if not paid_access_ok:
            failures.append(
                f"{symbol} ({dataset_symbol_id}): authenticated non-month-start probe "
                f"on {paid_probe_day} returned {paid_probe['status']} "
                f"({paid_probe['rows']} rows); continuous history entitlement is not confirmed"
            )
    result = {
        "source": "Tardis.dev",
        "exchange": EXCHANGE,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "exchange_available_since": exchange.get("availableSince"),
        "exchange_metadata_fields": sorted(exchange.keys()),
        "dataset_metadata_fields": sorted(datasets.keys()),
        "dataset_symbol_count": len(metadata),
        "datasets_exported_from": datasets.get("exportedFrom"),
        "datasets_exported_until": datasets.get("exportedUntil"),
        "entitlement_summary": entitlement_summary,
        "checks": checks,
        "status": "pass" if not failures else "fail",
        "historical_entitlement_note": "Public first-of-month samples validate the CSV path/schema only. Authenticated non-month-start probes are required to confirm continuous daily history entitlement.",
        "failures": failures,
    }
    print(json.dumps(result, indent=2), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
