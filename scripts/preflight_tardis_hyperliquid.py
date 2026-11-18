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
from datetime import datetime, timezone
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
        if exc.code == 404:
            return {"status": "not_found", "rows": 0, "url": url}
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
        date = available_since[:10]
        sample = get_dataset_sample(symbol, date, key)
        ok = sample["status"] == "ok" and sample["rows"] > 0
        checks.append({
            "symbol": symbol,
            "available_since": available_since,
            "dataset_types": item.get("dataTypes", []),
            "sample_date": date,
            "sample": sample,
            "status": "pass" if ok else "sample_unavailable",
        })
        if not ok:
            failures.append(f"{symbol}: trades sample on {date} returned {sample['status']} ({sample['rows']} rows)")
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
        "failures": failures,
    }
    print(json.dumps(result, indent=2), flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
