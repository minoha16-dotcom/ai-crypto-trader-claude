"""Fetch point-in-time Hyperliquid candles from Vike and run per-group WF reports.

Requires VIKE_API_KEY in the local environment. Never commit the key or data files.
The requested 2026-10-16..2026-11-16 UTC interval is scored; preceding candles
are warm-up only. This is research-only for Hyperliquid until a live adapter exists.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Running a script by path puts scripts/ (not the repository root) on sys.path.
# Add the project root so backend.* imports work consistently in GitHub Actions.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.strategy_research import evaluate_strategy_group, build_approval_artifact


API = "https://data.vike.io/v1/hyperliquid"
DEFAULT_START = "2026-10-16T00:00:00Z"
DEFAULT_END = "2026-11-16T00:00:00Z"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Dates must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def fetch_ndjson_candles(symbol, interval, start, end, api_key, market="perp"):
    params = urlencode({
        "symbol": symbol, "interval": interval, "market": market,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": 5000,
    })
    request = Request(
        f"{API}/ohlcv/export?{params}",
        headers={"X-API-KEY": api_key, "Accept": "application/x-ndjson"},
    )
    with urlopen(request, timeout=120) as response:
        raw = response.read().decode("utf-8")
    candles = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Vike NDJSON line {line_number}") from exc
        if not isinstance(row, dict) or "ts" not in row:
            # First line is the export metadata header.
            continue
        timestamp = int(row["ts"])
        candles.append([
            timestamp, float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]), float(row["volume"]),
        ])
    candles.sort(key=lambda row: row[0])
    if len(candles) < 2:
        raise ValueError(f"No usable {interval} candles returned for {symbol}")
    seen = set()
    for row in candles:
        if row[0] in seen:
            raise ValueError(f"Duplicate {interval} candle timestamp for {symbol}: {row[0]}")
        seen.add(row[0])
    return candles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframes", default="15m")
    parser.add_argument("--higher-timeframe", default="4h")
    parser.add_argument("--output-dir", default="data/strategy-research/hyperliquid")
    parser.add_argument("--reports", default="data/strategy-research/hyperliquid/reports.json")
    parser.add_argument("--approval-output", default="data/strategy-research/hyperliquid/approval-candidate.json")
    parser.add_argument("--max-folds", type=int, default=10)
    parser.add_argument("--direction-modes", default="BOTH,LONG,SHORT",
                        help="Comma-separated experiment modes: BOTH,LONG,SHORT")
    args = parser.parse_args()

    api_key = os.environ.get("VIKE_API_KEY", "").strip()
    if not api_key:
        parser.error("VIKE_API_KEY is required; set it locally and do not paste it into chat or commit it.")
    start, end = parse_utc(args.start), parse_utc(args.end)
    if end <= start:
        parser.error("--end must be later than --start")

    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    timeframes = [item.strip() for item in args.timeframes.split(",") if item.strip()]
    direction_modes = [item.strip().upper() for item in args.direction_modes.split(",") if item.strip()]
    if not direction_modes or any(mode not in ("BOTH", "LONG", "SHORT") for mode in direction_modes):
        parser.error("--direction-modes must contain only BOTH, LONG and/or SHORT")
    if len(set(direction_modes)) != len(direction_modes):
        parser.error("--direction-modes must not contain duplicates")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = []

    for symbol in symbols:
        for timeframe in timeframes:
            interval_minutes = {
                "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440,
            }.get(timeframe)
            if interval_minutes is None:
                raise ValueError(f"Unsupported research timeframe: {timeframe}")
            interval = timedelta(minutes=interval_minutes)
            warmup = 200
            lower_start = start - interval * warmup
            print(f"Fetching Hyperliquid {symbol} perp {timeframe}: {lower_start} -> {end}", flush=True)
            candles = fetch_ndjson_candles(
                symbol, timeframe, lower_start, end, api_key, market="perp"
            )
            higher = None
            if timeframe == "15m":
                higher_start = start - timedelta(hours=4 * 200)
                higher = fetch_ndjson_candles(
                    symbol, args.higher_timeframe, higher_start, end, api_key, market="perp"
                )
            cache_path = output_dir / f"{symbol}_{timeframe}.json"
            cache_path.write_text(json.dumps({
                "exchange": "hyperliquid", "symbol": symbol,
                "market_type": "futures", "timeframe": timeframe,
                "start_utc": args.start, "end_utc": args.end,
                "candles": candles, "higher_timeframe": args.higher_timeframe,
                "higher_timeframe_candles": higher,
                "source": "Vike Hyperliquid OHLCV export",
            }, separators=(",", ":")), encoding="utf-8")

            # Keep only 200 lower-timeframe warm-up bars before the requested
            # scoring window; do not silently score the earlier 4h warm-up span.
            lower_scoring = [row for row in candles if row[0] >= int(start.timestamp() * 1000)]
            lower_warmup = [row for row in candles if row[0] < int(start.timestamp() * 1000)][-warmup:]
            if len(lower_warmup) < warmup:
                raise ValueError(
                    f"{symbol} {timeframe}: need {warmup} lower-timeframe warm-up bars, "
                    f"received {len(lower_warmup)}"
                )
            evaluation_candles = lower_warmup + lower_scoring
            for mode in direction_modes:
                report = evaluate_strategy_group(
                    exchange="hyperliquid", symbol=symbol, market_type="futures",
                    timeframe=timeframe, candles=evaluation_candles,
                    higher_timeframe_candles=higher,
                    higher_timeframe=args.higher_timeframe,
                    start_utc=args.start, end_utc=args.end,
                    max_folds=args.max_folds,
                    direction_filter=None if mode == "BOTH" else mode,
                )
                reports.append(report)
                print(
                    f"{symbol} {timeframe} {mode}: {report['validation_status']} | "
                    f"qualified={report['qualified_examples']} | folds={report['walk_forward_folds']} | "
                    f"OOS={report['oos_trade_count']} | expectancy_R={report['net_expectancy_r']} | "
                    f"uplift_CI_low={report['uplift'].get('ci_lower')} | "
                    f"reasons={','.join(report['rejection_reasons']) or 'none'}",
                    flush=True,
                )

    Path(args.reports).parent.mkdir(parents=True, exist_ok=True)
    Path(args.reports).write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    # Hyperliquid is research-only in this app: it is not one of the configured
    # live exchange adapters, so this candidate file is never copied to the
    # active backend/strategy_approval.json by this collector.
    approval = build_approval_artifact(reports)
    approval["status"] = "research_only"
    approval["approved_groups"] = []
    approval["note"] = (
        "Research results only. Hyperliquid is not a configured live adapter; "
        "no active scanner approvals are emitted by this collector."
    )
    Path(args.approval_output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.approval_output).write_text(json.dumps(approval, indent=2), encoding="utf-8")
    print(f"Reports: {args.reports}")
    print(f"Research-only candidate artifact: {args.approval_output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
