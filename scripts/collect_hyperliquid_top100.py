"""Collect point-in-time top-100 Hyperliquid wallet fills and funding from Vike.

Selection is anchored to the 2026-10-15 23:00 UTC pnl_cohort label (carried by
the 2026-10-16 00:00 UTC positions snapshot), then ranked by realized PnL over
the preceding 30 UTC days. Target fills/funding cover [2026-10-16, 2026-11-16).
Requires VIKE_API_KEY and the optional research dependency Polars.
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API = "https://data.vike.io/v1/hyperliquid"
SELECTION_LABEL_HOUR = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)
TARGET_START = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)
TARGET_END = datetime(2026, 11, 16, 0, tzinfo=timezone.utc)
PRIOR_START = TARGET_START - timedelta(days=30)


def api_json(path, params, api_key):
    query = urlencode(params)
    request = Request(
        f"{API}/{path}?{query}",
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
    )
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url, destination):
    request = Request(url, headers={"Accept": "application/octet-stream"})
    with urlopen(request, timeout=300) as response, open(destination, "wb") as output:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            output.write(chunk)


def archive_file(dataset, day, api_key, destination):
    response = api_json("archive/url", {"dataset": dataset, "date": day.isoformat()}, api_key)
    url = response.get("url")
    if not url:
        raise RuntimeError(f"Vike archive did not return a URL for {dataset} {day}")
    download(url, destination)
    return response


def date_range(start, end):
    current = start.date()
    while current < end.date():
        yield current
        current += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data/strategy-research/hyperliquid/wallets")
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--request-pause", type=float, default=0.35)
    args = parser.parse_args()
    if args.top_n != 100:
        parser.error("The registered research protocol fixes the cohort at exactly 100 wallets.")
    api_key = os.environ.get("VIKE_API_KEY", "").strip()
    if not api_key:
        parser.error("VIKE_API_KEY is required; set it locally and do not paste it into chat.")
    if args.request_pause < 0.3:
        parser.error("Use at least 0.3 seconds between per-wallet API calls (200/minute budget).")

    try:
        import polars as pl
    except ImportError as exc:
        raise SystemExit(
            "Install optional research dependencies first: "
            "python -m pip install -r requirements-research.txt"
        ) from exc

    root = Path(args.output_dir)
    raw = root / "raw"
    selected_dir = root / "selected"
    raw.mkdir(parents=True, exist_ok=True)
    selected_dir.mkdir(parents=True, exist_ok=True)

    # Point-in-time cohort label: positions rows in the 00:00 UTC hour on
    # Oct 16 carry pnl_cohort graded strictly before 23:00 UTC on Oct 15.
    positions_path = raw / "positions-label-2026-10-16.parquet"
    archive_file("positions", date(2026, 10, 16), api_key, positions_path)
    positions = pl.read_parquet(positions_path)
    label_rows = positions.filter(
        (pl.col("snapshot_ts") >= SELECTION_LABEL_HOUR)
        & (pl.col("snapshot_ts") < SELECTION_LABEL_HOUR + timedelta(hours=1))
        & pl.col("pnl_cohort").is_not_null()
        & (pl.col("pnl_cohort") != "unknown")
    ).select("wallet", "pnl_cohort").unique()
    if label_rows.is_empty():
        raise RuntimeError("No point-in-time pnl_cohort labels found for 2026-10-15 23:00 UTC")

    # Rank only wallets carrying a known point-in-time PnL cohort, using fills
    # closed during the immediately preceding 30 UTC days.
    prior_totals = []
    for day in date_range(PRIOR_START, TARGET_START):
        path = raw / f"fills-prior-{day.isoformat()}.parquet"
        archive_file("fills", day, api_key, path)
        frame = pl.scan_parquet(path).select("wallet", "closed_pnl").group_by("wallet").agg(
            pl.col("closed_pnl").sum().alias("prior_30d_realized_pnl")
        ).collect(engine="streaming")
        prior_totals.append(frame)
        path.unlink(missing_ok=True)
        time.sleep(args.request_pause)
    pnl_by_wallet = pl.concat(prior_totals).group_by("wallet").agg(
        pl.col("prior_30d_realized_pnl").sum()
    )
    ranked = (
        label_rows.join(pnl_by_wallet, on="wallet", how="inner")
        .filter(pl.col("prior_30d_realized_pnl") > 0)
        .sort("prior_30d_realized_pnl", descending=True)
        .unique(subset=["wallet"], keep="first", maintain_order=True)
        .head(args.top_n)
    )
    if ranked.height < args.top_n:
        raise RuntimeError(
            f"Only {ranked.height} positively profitable, cohort-labelled wallets were found; "
            f"need {args.top_n}. No smaller cohort will be silently substituted."
        )
    ranked.write_parquet(root / "selected_top100_wallets.parquet")
    wallets = ranked.to_dicts()
    addresses = [row["wallet"] for row in wallets]
    (root / "selected_top100_wallets.json").write_text(
        json.dumps({
            "selection_label_hour_utc": "2026-10-15T23:00:00Z",
            "prior_pnl_window_start_utc": "2026-09-16T00:00:00Z",
            "prior_pnl_window_end_utc": "2026-10-16T00:00:00Z",
            "selection_rule": "known point-in-time pnl_cohort, then top 100 positive realized PnL",
            "wallets": wallets,
        }, indent=2),
        encoding="utf-8",
    )
    wallet_set = pl.Series("wallet", addresses)

    # Opening positions: latest recorded snapshot strictly before target start.
    opening_path = raw / "positions-opening-2026-10-15.parquet"
    archive_file("positions", date(2026, 10, 15), api_key, opening_path)
    opening = pl.read_parquet(opening_path).filter(
        (pl.col("snapshot_ts") < TARGET_START) & pl.col("wallet").is_in(wallet_set)
    )
    if opening.is_empty():
        opening_ts = None
        opening.write_parquet(selected_dir / "opening_positions.parquet")
    else:
        latest_by_position = opening.group_by("wallet", "asset").agg(
            pl.col("snapshot_ts").max().alias("snapshot_ts")
        )
        opening = opening.join(
            latest_by_position, on=["wallet", "asset", "snapshot_ts"], how="inner"
        )
        opening_ts = opening.select(pl.col("snapshot_ts").max()).item()
        opening.write_parquet(selected_dir / "opening_positions.parquet")

    # Download and filter every fill in the requested 31-day target period.
    selected_fill_rows = 0
    for day in date_range(TARGET_START, TARGET_END):
        path = raw / f"fills-target-{day.isoformat()}.parquet"
        archive_file("fills", day, api_key, path)
        selected = pl.scan_parquet(path).filter(
            pl.col("wallet").is_in(wallet_set)
        ).collect(engine="streaming")
        if not selected.is_empty():
            selected.write_parquet(selected_dir / f"fills-{day.isoformat()}.parquet")
            selected_fill_rows += selected.height
        path.unlink(missing_ok=True)
        time.sleep(args.request_pause)

    # Wallet-specific funding payments, paged until the API cursor is exhausted.
    funding_dir = selected_dir / "funding"
    funding_dir.mkdir(parents=True, exist_ok=True)
    funding_rows = 0
    start_text = "2026-10-16T00:00:00Z"
    end_text = "2026-11-16T00:00:00Z"
    for index, address in enumerate(addresses, 1):
        rows, cursor = [], None
        while True:
            params = {"start": start_text, "end": end_text, "limit": 5000}
            if cursor:
                params["cursor"] = cursor
            page = api_json(f"wallets/{address}/funding", params, api_key)
            rows.extend(page.get("items", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            time.sleep(args.request_pause)
        funding_rows += len(rows)
        (funding_dir / f"{address}.json").write_text(
            json.dumps({"wallet": address, "items": rows}, separators=(",", ":")),
            encoding="utf-8",
        )
        if index % 10 == 0:
            print(f"Funding collected for {index}/{len(addresses)} wallets", flush=True)
        time.sleep(args.request_pause)

    manifest = {
        "source": "Vike Hyperliquid archive and wallet funding API",
        "selection_label_hour_utc": "2026-10-15T23:00:00Z",
        "selection_rule": "point-in-time pnl_cohort not unknown; top 100 by prior 30d positive realized PnL",
        "prior_pnl_window_utc": ["2026-09-16T00:00:00Z", "2026-10-16T00:00:00Z"],
        "target_window_utc": ["2026-10-16T00:00:00Z", "2026-11-16T00:00:00Z"],
        "selected_wallet_count": len(addresses),
        "opening_positions_snapshot_ts": str(opening_ts),
        "target_fill_rows": selected_fill_rows,
        "funding_rows": funding_rows,
        "files": {
            "wallets": "selected_top100_wallets.json",
            "opening_positions": "selected/opening_positions.parquet",
            "target_fills": "selected/fills-YYYY-MM-DD.parquet",
            "funding": "selected/funding/<wallet>.json",
        },
        "limitations": [
            "Funding payments are collected separately and must be joined by wallet, symbol and UTC time.",
            "The opening snapshot seeds position reconstruction; fills after the snapshot must be replayed in timestamp/trade_id order.",
            "The archive may contain reconstructed hours; inspect its manifest metadata before interpreting margin or liquidation fields.",
            "This is personal research data under the provider's archive terms; do not redistribute.",
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
