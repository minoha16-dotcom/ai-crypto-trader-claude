#!/usr/bin/env python3
"""Collect a point-in-time Hyperliquid trader cohort and its historical fills from Vike.

The cohort is selected from Vike's historical positions archive in the UTC hour
immediately before the requested observation window. The archived pnl_cohort
label is defined at the start of that clock hour, so it does not use outcomes
from the observation window. Vike archives are processed one day at a time;
raw provider files are deleted immediately after filtering.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

import polars as pl
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

# Ensure imports work both when run as a script and when imported by tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fetch_hyperliquid_leader_fills import (
    _fetch_historical_orders,
    _fetch_time_range,
    _fetch_twap_slice_fills,
)

API_BASE = "https://data.vike.io/v1/hyperliquid"
DATASETS_URL = f"{API_BASE}/archive/datasets"
ARCHIVE_URL = f"{API_BASE}/archive/url"
ADDRESS_RE = re.compile(r"^0x[a-f0-9]{40}$")
FILL_COLUMNS = (
    "fill_ts", "wallet", "asset", "side", "size_token", "price", "fee",
    "fee_token", "order_id", "trade_id", "closed_pnl", "start_position",
)
DEFAULT_COHORT = "3x smart"
START_POSITION_AVAILABLE_FROM_MS = int(
    datetime(2026, 9, 29, tzinfo=timezone.utc).timestamp() * 1000
)
VENDOR_LICENSE_NOTE = (
    "Vike Hyperliquid historical data: personal use under Vike Terms of Service; "
    "commercial use requires written agreement. Do not redistribute raw archives. "
    "Hyperliquid funding/order/TWAP evidence is retrieved from the public Info API."
)


def _utc_ms(value: str, field: str) -> int:
    raw = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _as_ms(value: Any, field: str) -> int:
    if value is None:
        raise ValueError(f"{field} is missing")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    return _utc_ms(str(value), field)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _history_start_ms(start_ms: int, history_warmup_days: int) -> int:
    history_warmup_days = int(history_warmup_days)
    if history_warmup_days < 1 or history_warmup_days > 180:
        raise ValueError("history_warmup_days must be between 1 and 180")
    history_start = int(start_ms) - history_warmup_days * 24 * 60 * 60 * 1000
    if history_start < START_POSITION_AVAILABLE_FROM_MS:
        raise ValueError(
            "the requested warm-up starts before Vike's start_position field "
            "was available (2026-09-29); reduce history_warmup_days"
        )
    return history_start


def _session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "ai-crypto-trader-vike-research/1.0"})
    return session


def _vike_json(session: requests.Session, url: str, api_key: str, params=None):
    response = session.get(
        url,
        params=params,
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
        timeout=(15, 90),
    )
    if response.status_code == 401:
        raise RuntimeError("Vike rejected VIKE_API_KEY (HTTP 401). Check the repository/local secret.")
    if response.status_code == 403:
        raise RuntimeError("Vike denied this archive request (HTTP 403); check account plan and access.")
    response.raise_for_status()
    try:
        return response.content, response.json()
    except ValueError as exc:
        raise RuntimeError("Vike returned a non-JSON response for a metadata request") from exc


def _archive_download(
    session: requests.Session,
    api_key: str,
    dataset: str,
    day: str,
    target: Path,
    expected_bytes: int | None = None,
) -> tuple[int, str, dict[str, Any]]:
    _, link_document = _vike_json(
        session,
        ARCHIVE_URL,
        api_key,
        params={"dataset": dataset, "date": day},
    )
    signed_url = link_document.get("url")
    if not isinstance(signed_url, str) or not signed_url.startswith("https://"):
        raise RuntimeError(f"Vike did not return a valid signed download link for {dataset} {day}")

    # The signed URL is a bearer credential. Never print or store it in metadata.
    try:
        response = session.get(signed_url, stream=True, timeout=(20, 300))
        response.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" (HTTP {status})" if status else ""
        # Do not surface the signed URL: it is itself a temporary bearer token.
        raise RuntimeError(f"Vike archive download failed for {dataset} {day}{suffix}") from None
    digest = hashlib.sha256()
    written = 0
    try:
        with target.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                output.write(chunk)
                digest.update(chunk)
                written += len(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        response.close()

    if expected_bytes is not None and written != int(expected_bytes):
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"archive byte count mismatch for {dataset} {day}: "
            f"catalogue={expected_bytes}, downloaded={written}"
        )
    if link_document.get("bytes") is not None and written != int(link_document["bytes"]):
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Vike signed-link byte count mismatch for {dataset} {day}")
    return written, digest.hexdigest(), link_document


def _select_wallets_from_positions(
    positions: pl.LazyFrame,
    *,
    selection_start_ms: int,
    selection_end_ms: int,
    cohort_label: str,
    wallet_limit: int,
) -> list[str]:
    start = datetime.fromtimestamp(selection_start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(selection_end_ms / 1000, tz=timezone.utc)
    selected = (
        positions
        .filter(
            (pl.col("snapshot_ts") >= pl.lit(start))
            & (pl.col("snapshot_ts") < pl.lit(end))
            & (pl.col("pnl_cohort") == cohort_label)
        )
        .select("wallet")
        .drop_nulls()
        .unique()
        .collect(engine="streaming")
    )
    wallets = sorted(
        {
            str(row[0]).lower()
            for row in selected.iter_rows()
            if ADDRESS_RE.fullmatch(str(row[0]).lower())
        },
        key=lambda address: hashlib.sha256(
            ("ai-crypto-v1|" + address).encode("ascii")
        ).hexdigest(),
    )
    return wallets[:wallet_limit]


def _write_snapshot(
    *,
    session: requests.Session,
    api_key: str,
    start_ms: int,
    end_ms: int,
    cohort_label: str,
    wallet_limit: int,
    collector_code_sha: str,
    output: Path,
) -> None:
    selection_start_ms = start_ms - 60 * 60 * 1000
    selection_end_ms = start_ms
    selection_hour = datetime.fromtimestamp(
        selection_start_ms / 1000, tz=timezone.utc
    ).replace(minute=0, second=0, microsecond=0)
    if int(selection_hour.timestamp() * 1000) != selection_start_ms:
        raise SystemExit("The observation start must be on a UTC hour boundary.")

    metadata_bytes, catalogue = _vike_json(session, DATASETS_URL, api_key)
    catalogue_sha = hashlib.sha256(metadata_bytes).hexdigest()
    source_day = selection_hour.date().isoformat()
    day_entry = next(
        (row for row in catalogue.get("dates", []) if row.get("date") == source_day),
        None,
    )
    if not day_entry or not isinstance(day_entry.get("streams", {}).get("positions"), dict):
        raise RuntimeError(f"Vike has no historical positions archive for {source_day}")
    expected_bytes = int(day_entry["streams"]["positions"].get("bytes") or 0)

    with tempfile.TemporaryDirectory(prefix="vike-cohort-") as temp_dir:
        positions_path = Path(temp_dir) / f"positions-{source_day}.parquet"
        retrieved_at = datetime.now(timezone.utc)
        written, positions_sha, link = _archive_download(
            session, api_key, "positions", source_day, positions_path, expected_bytes
        )
        lazy = pl.scan_parquet(positions_path)
        schema = lazy.collect_schema().names()
        needed = {"wallet", "snapshot_ts", "pnl_cohort"}
        if not needed.issubset(schema):
            raise RuntimeError(
                "Vike positions archive is missing required cohort columns: "
                + ", ".join(sorted(needed - set(schema)))
            )
        wallets = _select_wallets_from_positions(
            lazy,
            selection_start_ms=selection_start_ms,
            selection_end_ms=selection_end_ms,
            cohort_label=cohort_label,
            wallet_limit=wallet_limit,
        )
    if not wallets:
        raise RuntimeError(
            f"No wallets with pnl_cohort={cohort_label!r} were open in the hour "
            f"before { _iso(start_ms) }. No cohort snapshot was created."
        )

    snapshot = {
        "snapshot_version": 1,
        "snapshot_only": True,
        "snapshot_kind": "historical_archive_causal",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "wallets": wallets,
        "manifest": {
            "source_provider": "Vike Hyperliquid historical positions archive",
            "source_url": ARCHIVE_URL,
            "source_metadata_url": DATASETS_URL,
            "source_license": VENDOR_LICENSE_NOTE,
            "source_snapshot_utc": _iso(selection_start_ms),
            "leaderboard_retrieved_at_utc": retrieved_at.isoformat(),
            "source_snapshot_sha256": positions_sha,
            "source_metadata_sha256": catalogue_sha,
            "cohort_selection_as_of_utc": _iso(selection_start_ms),
            "cohort_selection_method": f"historical_positions.pnl_cohort={cohort_label}",
            "cohort_selection_label": cohort_label,
            "cohort_selection_label_basis": "start_of_snapshot_clock_hour; fills strictly before that hour",
            "cohort_selection_hour_start_utc": _iso(selection_start_ms),
            "data_start_utc": _iso(start_ms),
            "data_end_utc": _iso(end_ms),
            "cohort_snapshot_code_sha": collector_code_sha.lower(),
            "wallets_selected": len(wallets),
            "wallet_limit": wallet_limit,
            "positions_archive_date": source_day,
            "positions_archive_bytes": written,
            "positions_archive_rows": link.get("rows"),
            "read_only_public_data": True,
            "not_model_ready": True,
        },
        "limitations": [
            "The cohort is the Vike point-in-time 3x smart pnl cohort, not a claim of verified future skill.",
            "The source cohort is limited to wallets with an open position in the hour before the observation window.",
            "The archived pnl_cohort field is defined at the start of the row's clock hour and uses fills strictly before that hour.",
            "The exact historical parquet file is represented by SHA-256; the raw source file is not retained in this snapshot.",
            "Vike data is for personal use under its current Terms of Service; do not redistribute raw data.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"SNAPSHOT {output}")
    print(f"SNAPSHOT_SHA256 {_sha256_file(output)}")
    print(f"COHORT_LABEL {cohort_label}")
    print(f"COHORT_AS_OF_UTC {_iso(selection_start_ms)}")
    print(f"WALLETS_SELECTED {len(wallets)}")


def _fill_direction(start_position: Any, size: Any, side: str) -> str:
    if start_position is None:
        return "UNCLASSIFIED"
    before = float(start_position)
    quantity = abs(float(size))
    if quantity <= 0 or side not in ("buy", "sell"):
        return "UNCLASSIFIED"
    after = before + (quantity if side == "buy" else -quantity)
    eps = max(1e-10, abs(before) * 1e-9)
    if abs(before) <= eps:
        return "Open Long" if after > 0 else "Open Short" if after < 0 else "UNCLASSIFIED"
    if before > 0:
        return "Add Long" if after > before + eps else "Close Long"
    return "Add Short" if after < before - eps else "Close Short"


def _native_fill(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = _as_ms(row.get("fill_ts"), "fill_ts")
    start_position = row.get("start_position")
    return {
        "coin": str(row.get("asset") or ""),
        "time": timestamp,
        "px": float(row["price"]),
        "sz": float(row["size_token"]),
        "dir": _fill_direction(start_position, row["size_token"], str(row.get("side") or "").lower()),
        "oid": row.get("order_id"),
        "tid": row.get("trade_id"),
        "fee": float(row["fee"]),
        "feeToken": str(row.get("fee_token") or ""),
        "closedPnl": float(row.get("closed_pnl") or 0.0),
        "startPosition": None if start_position is None else float(start_position),
        "_source": "vike_hyperliquid_daily_fills_archive",
    }


def _daily_dates(start_ms: int, end_ms: int) -> list[str]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date()
    end = datetime.fromtimestamp((end_ms - 1) / 1000, tz=timezone.utc).date()
    result = []
    current = start
    while current <= end:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _append_fills_for_day(
    *,
    parquet_path: Path,
    selected_wallets: list[str],
    spool_dir: Path,
    first_seen_assets: dict[str, set[str]],
    initial_nonzero_positions: dict[str, int],
) -> tuple[int, int, dict[str, int]]:
    lazy = pl.scan_parquet(parquet_path)
    schema = set(lazy.collect_schema().names())
    missing = set(FILL_COLUMNS) - schema
    if missing:
        raise RuntimeError("Vike fills archive is missing columns: " + ", ".join(sorted(missing)))
    filtered = (
        lazy.filter(pl.col("wallet").is_in(selected_wallets))
        .select(FILL_COLUMNS)
        .collect(engine="streaming")
    )
    wallet_rows: dict[str, list[dict[str, Any]]] = {}
    invalid_start_position: dict[str, int] = {}
    total = 0
    for row in filtered.iter_rows(named=True):
        address = str(row.get("wallet") or "").lower()
        if address not in selected_wallets:
            continue
        asset = str(row.get("asset") or "")
        if not asset:
            continue
        total += 1
        if row.get("start_position") is None:
            invalid_start_position[address] = invalid_start_position.get(address, 0) + 1
        # The first observed fill per wallet/asset must start from flat. A
        # non-zero start means the position predates our warm-up archive and
        # cannot be reconstructed with an auditable entry/fee lifecycle.
        seen_assets = first_seen_assets.setdefault(address, set())
        if asset not in seen_assets:
            seen_assets.add(asset)
            try:
                first_start = float(row.get("start_position"))
            except (TypeError, ValueError, OverflowError):
                first_start = float("nan")
            if not (abs(first_start) <= 1e-10):
                initial_nonzero_positions[address] = initial_nonzero_positions.get(address, 0) + 1
        wallet_rows.setdefault(address, []).append(_native_fill(row))

    # Append small wallet batches, not the full market-wide archive.
    for address, rows in wallet_rows.items():
        path = spool_dir / f"{address}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
    return total, invalid_start_position


def _write_raw_dataset(
    *,
    output: Path,
    wallets: list[str],
    spool_dir: Path,
    wallet_metadata: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        handle.write('{"dataset_version":2,"created_at_utc":')
        handle.write(json.dumps(datetime.now(timezone.utc).isoformat()))
        handle.write(',"manifest":')
        json.dump(manifest, handle, ensure_ascii=False, allow_nan=False)
        handle.write(',"wallets":[')
        for index, address in enumerate(wallets):
            if index:
                handle.write(",")
            meta = wallet_metadata[address]
            handle.write('{"address":')
            handle.write(json.dumps(address))
            handle.write(',"rank":')
            handle.write(str(index + 1))
            handle.write(',"fills":[')
            spool = spool_dir / f"{address}.jsonl"
            first = True
            if spool.exists():
                with spool.open("r", encoding="utf-8") as source:
                    for line in source:
                        if not line.strip():
                            continue
                        if not first:
                            handle.write(",")
                        handle.write(line.strip())
                        first = False
            handle.write('],"funding":')
            json.dump(meta["funding"], handle, ensure_ascii=False, allow_nan=False)
            handle.write(',"twap_slice_fills":')
            json.dump(meta["twap_slice_fills"], handle, ensure_ascii=False, allow_nan=False)
            handle.write(',"historical_orders":')
            json.dump(meta["historical_orders"], handle, ensure_ascii=False, allow_nan=False)
            for key in (
                "complete", "training_eligible", "fill_history", "funding_history",
                "twap_history", "historical_orders_status",
            ):
                handle.write("," + json.dumps(key) + ":")
                json.dump(meta[key], handle, ensure_ascii=False, allow_nan=False)
            handle.write("}")
        handle.write("]}\n")


def _collect(
    *,
    session: requests.Session,
    api_key: str,
    start_ms: int,
    end_ms: int,
    snapshot_path: Path,
    collector_code_sha: str,
    output: Path,
    pause_seconds: float,
    history_warmup_days: int,
) -> None:
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_sha = hashlib.sha256(snapshot_bytes).hexdigest()
    try:
        snapshot = json.loads(snapshot_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid cohort snapshot JSON: {exc}") from exc
    if snapshot.get("snapshot_only") is not True or snapshot.get("snapshot_kind") != "historical_archive_causal":
        raise SystemExit("--snapshot-file must be created by this collector's historical --snapshot-only stage")
    frozen = snapshot.get("manifest") or {}
    if _utc_ms(frozen.get("data_start_utc", ""), "snapshot.data_start_utc") != start_ms:
        raise SystemExit("collection start must match the frozen snapshot")
    if _utc_ms(frozen.get("data_end_utc", ""), "snapshot.data_end_utc") != end_ms:
        raise SystemExit("collection end must match the frozen snapshot")
    cohort_ms = _utc_ms(frozen.get("cohort_selection_as_of_utc", ""), "cohort_selection_as_of_utc")
    if cohort_ms >= start_ms:
        raise SystemExit("historical cohort label is not strictly earlier than the observation window")
    wallets = [str(value).lower() for value in snapshot.get("wallets", [])]
    if not wallets or any(not ADDRESS_RE.fullmatch(value) for value in wallets):
        raise SystemExit("snapshot contains no valid wallet addresses")

    catalogue_bytes, catalogue = _vike_json(session, DATASETS_URL, api_key)
    catalogue_sha = hashlib.sha256(catalogue_bytes).hexdigest()
    if catalogue_sha != frozen.get("source_metadata_sha256"):
        # The daily catalogue changes over time as more days appear. We retain
        # the original positions-catalogue hash and separately record this
        # current fills-catalogue hash; the two are not expected to be equal.
        current_catalogue_sha = catalogue_sha
    else:
        current_catalogue_sha = catalogue_sha

    history_start_ms = _history_start_ms(start_ms, history_warmup_days)
    daily_dates = _daily_dates(history_start_ms, end_ms)
    by_date = {str(row.get("date")): row for row in catalogue.get("dates", [])}
    missing_dates = [
        day for day in daily_dates
        if day not in by_date or not isinstance(by_date[day].get("streams", {}).get("fills"), dict)
    ]
    if missing_dates:
        raise RuntimeError(
            "Vike fills archive is not complete for requested dates: "
            + ", ".join(missing_dates[:10])
        )

    wallet_stats = {
        address: {
            "fills_rows": 0,
            "missing_start_position": 0,
            "funding": [],
            "twap_slice_fills": [],
            "historical_orders": [],
            "complete": False,
        }
        for address in wallets
    }
    archive_records = []
    downloaded_rows = 0
    first_seen_assets: dict[str, set[str]] = {}
    initial_nonzero_positions: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="vike-fills-") as temp_dir:
        temp = Path(temp_dir)
        spool_dir = temp / "wallet-fills"
        spool_dir.mkdir()
        for index, day in enumerate(daily_dates, start=1):
            fill_stream = by_date[day]["streams"]["fills"]
            expected_bytes = int(fill_stream.get("bytes") or 0)
            raw_path = temp / f"fills-{day}.parquet"
            started = time.monotonic()
            written, day_sha, link = _archive_download(
                session, api_key, "fills", day, raw_path, expected_bytes
            )
            day_rows, missing_start = _append_fills_for_day(
                parquet_path=raw_path,
                selected_wallets=wallets,
                spool_dir=spool_dir,
                first_seen_assets=first_seen_assets,
                initial_nonzero_positions=initial_nonzero_positions,
            )
            raw_path.unlink(missing_ok=True)
            downloaded_rows += day_rows
            for address, count in missing_start.items():
                wallet_stats[address]["missing_start_position"] += count
            archive_records.append({
                "date": day,
                "archive_bytes": written,
                "archive_sha256": day_sha,
                "archive_rows_catalogue": fill_stream.get("rows"),
                "selected_wallet_fill_rows": day_rows,
                "download_seconds": round(time.monotonic() - started, 3),
            })
            print(
                f"ARCHIVE_DAY {index}/{len(daily_dates)} {day}: "
                f"{day_rows} selected fills; raw file deleted"
            )

        # The archived fills supply every fill for the selected wallets. Funding,
        # TWAP slice and historical-order evidence is obtained from Hyperliquid's
        # public Info API, preserving the existing conservative reconstruction.
        from_ms = start_ms
        for index, address in enumerate(wallets, start=1):
            fills_path = spool_dir / f"{address}.jsonl"
            fill_rows = 0
            if fills_path.exists():
                with fills_path.open("r", encoding="utf-8") as handle:
                    for _ in handle:
                        fill_rows += 1
            wallet_stats[address]["fills_rows"] = fill_rows
            funding, funding_status = _fetch_time_range(
                address,
                "userFunding",
                start_ms,
                end_ms,
                timeout=60.0,
                pause_seconds=pause_seconds,
            )
            if pause_seconds:
                time.sleep(pause_seconds)
            twap_fills, twap_status = _fetch_twap_slice_fills(
                address, start_ms, end_ms, timeout=60.0
            )
            # Vike's daily fill archive can already include TWAP executions.
            # Deduplicate the separate native slice feed by the exchange fill
            # identity before the preparer reconstructs positions.
            known_keys = set()
            if fills_path.exists():
                with fills_path.open("r", encoding="utf-8") as source:
                    for line in source:
                        if not line.strip():
                            continue
                        archived = json.loads(line)
                        known_keys.add((
                            str(archived.get("tid") or ""),
                            str(archived.get("coin") or ""),
                            int(archived.get("time") or 0),
                            str(archived.get("oid") or ""),
                        ))
            deduplicated_twap = []
            duplicate_twap_rows = 0
            for wrapper in twap_fills:
                fill = wrapper.get("fill") if isinstance(wrapper, dict) else None
                if not isinstance(fill, dict):
                    deduplicated_twap.append(wrapper)
                    continue
                key = (
                    str(fill.get("tid") or ""),
                    str(fill.get("coin") or ""),
                    int(fill.get("time") or 0),
                    str(fill.get("oid") or ""),
                )
                if key in known_keys:
                    duplicate_twap_rows += 1
                    continue
                known_keys.add(key)
                deduplicated_twap.append(wrapper)
            twap_fills = deduplicated_twap
            twap_status["duplicate_rows_already_in_vike_archive"] = duplicate_twap_rows
            if pause_seconds:
                time.sleep(pause_seconds)
            orders, order_status = _fetch_historical_orders(address, timeout=60.0)
            fill_complete = (
                wallet_stats[address]["missing_start_position"] == 0
                and initial_nonzero_positions.get(address, 0) == 0
            )
            complete = (
                fill_complete
                and funding_status.get("complete", False)
                and twap_status.get("complete", False)
                and order_status.get("complete_within_documented_limit", False)
            )
            wallet_stats[address].update({
                "funding": funding,
                "twap_slice_fills": twap_fills,
                "historical_orders": orders,
                "complete": bool(complete),
                "funding_history": funding_status,
                "twap_history": {
                    **twap_status,
                    "source_note": "Hyperliquid Info API userTwapSliceFills endpoint",
                },
                "historical_orders_status": order_status,
                "fill_history": {
                    "requests": len(daily_dates),
                    "rows": fill_rows,
                    "daily_archive_files": len(daily_dates),
                    "daily_archive_files_complete": len(archive_records) == len(daily_dates),
                    "missing_start_position_rows": wallet_stats[address]["missing_start_position"],
                    "pre_window_positions_already_open_at_warmup_start": initial_nonzero_positions.get(address, 0),
                    "history_warmup_start_utc": _iso(history_start_ms),
                    "complete": fill_complete and len(archive_records) == len(daily_dates),
                    "source": "Vike daily fills Parquet archive",
                },
                "training_eligible": False,
            })
            print(
                f"WALLET {index}/{len(wallets)} {address[:10]}…: "
                f"{fill_rows} fills, {len(funding)} funding rows, "
                f"{len(orders)} orders, {'COMPLETE_RAW' if complete else 'INCOMPLETE_RAW'}"
            )

        retrieved_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            **frozen,
            "source_provider": "Vike Hyperliquid daily fills/positions archives + Hyperliquid public Info API",
            "source_url": ARCHIVE_URL,
            "source_metadata_url": DATASETS_URL,
            "source_license": VENDOR_LICENSE_NOTE,
            "source_snapshot_kind": "historical_archive_causal",
            "source_snapshot_utc": frozen["source_snapshot_utc"],
            "leaderboard_retrieved_at_utc": frozen["leaderboard_retrieved_at_utc"],
            "source_snapshot_sha256": frozen["source_snapshot_sha256"],
            "source_metadata_sha256": frozen["source_metadata_sha256"],
            "collection_catalogue_sha256": current_catalogue_sha,
            "collection_retrieved_at_utc": retrieved_at,
            "raw_daily_archive_files": archive_records,
            "raw_fills_rows_for_selected_wallets": downloaded_rows,
            "fills_archive_dates": daily_dates,
            "fills_history_start_utc": _iso(history_start_ms),
            "history_warmup_days": history_warmup_days,
            "data_start_utc": _iso(start_ms),
            "data_end_utc": _iso(end_ms),
            "collector_code_sha": collector_code_sha.lower(),
            "cohort_snapshot_code_sha": frozen["cohort_snapshot_code_sha"],
            "cohort_snapshot_file_sha256": snapshot_sha,
            "cohort_selection_as_of_utc": frozen["cohort_selection_as_of_utc"],
            "cohort_selection_method": frozen["cohort_selection_method"],
            "cohort_selection_label_basis": frozen["cohort_selection_label_basis"],
            "cohort_selection_wallet_count": len(wallets),
            "collector_run_at_utc": retrieved_at,
            "fills_source": "Vike daily fills parquet archives",
            "funding_request_type": "Hyperliquid Info API userFunding",
            "historical_orders_request_type": "Hyperliquid Info API historicalOrders",
            "twap_fills_request_type": "Hyperliquid Info API userTwapSliceFills",
            "read_only_public_data": True,
            "not_model_ready": True,
            "attribution": "Vike Hyperliquid historical archive; Hyperliquid public Info API",
        }
        _write_raw_dataset(
            output=output,
            wallets=wallets,
            spool_dir=spool_dir,
            wallet_metadata=wallet_stats,
            manifest=manifest,
        )

    print(f"OUTPUT {output}")
    print(f"RAW_DATASET_SHA256 {_sha256_file(output)}")
    complete_count = sum(1 for item in wallet_stats.values() if item["complete"])
    print(f"WALLETS_COMPLETE_RAW {complete_count}/{len(wallets)}")
    print(f"SELECTED_FILL_ROWS {downloaded_rows}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Observation start in UTC, ISO-8601")
    parser.add_argument("--end", required=True, help="Observation end in UTC, ISO-8601 (exclusive)")
    parser.add_argument("--collector-code-sha", required=True, help="Git commit SHA of this collector")
    parser.add_argument("--snapshot-only", action="store_true", help="Select and save the historical cohort only")
    parser.add_argument("--snapshot-file", help="Previously saved cohort JSON for the collection stage")
    parser.add_argument("--cohort-label", default=DEFAULT_COHORT, help="Vike point-in-time pnl_cohort label")
    parser.add_argument("--wallet-limit", type=int, default=100)
    parser.add_argument("--out", default="artifacts/hyperliquid-vike-raw-dataset.json")
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    parser.add_argument(
        "--history-warmup-days",
        type=int,
        default=17,
        help="Days of pre-window fills used only to reconstruct positions opened before the observation window.",
    )
    args = parser.parse_args()

    try:
        start_ms = _utc_ms(args.start, "start")
        end_ms = _utc_ms(args.end, "end")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if end_ms <= start_ms:
        raise SystemExit("--end must be later than --start")
    if start_ms % (60 * 60 * 1000) != 0:
        raise SystemExit("--start must be aligned to a UTC hour for causal cohort selection")
    if args.wallet_limit < 1 or args.wallet_limit > 1000:
        raise SystemExit("--wallet-limit must be between 1 and 1000")
    if args.pause_seconds < 0:
        raise SystemExit("--pause-seconds cannot be negative")
    try:
        _history_start_ms(start_ms, args.history_warmup_days)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", args.collector_code_sha):
        raise SystemExit("--collector-code-sha must be a 7-40 character Git SHA")
    if args.snapshot_only == bool(args.snapshot_file):
        raise SystemExit("choose exactly one of --snapshot-only or --snapshot-file")

    load_dotenv()
    api_key = os.environ.get("VIKE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("VIKE_API_KEY is not set in the environment; no data was requested")
    session = _session()
    output = Path(args.out)

    if args.snapshot_only:
        _write_snapshot(
            session=session,
            api_key=api_key,
            start_ms=start_ms,
            end_ms=end_ms,
            cohort_label=args.cohort_label,
            wallet_limit=args.wallet_limit,
            collector_code_sha=args.collector_code_sha,
            output=output,
        )
        return 0

    _collect(
        session=session,
        api_key=api_key,
        start_ms=start_ms,
        end_ms=end_ms,
        snapshot_path=Path(args.snapshot_file),
        collector_code_sha=args.collector_code_sha,
        output=output,
        pause_seconds=args.pause_seconds,
        history_warmup_days=args.history_warmup_days,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
