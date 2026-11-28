#!/usr/bin/env python3
"""Publish Vike raw-feature events to the AI Crypto Trader diagnostic Shadow API.

Input is JSON Lines. Each line must be one event payload accepted by
POST /api/ml-shadow/predict. This transport does not calculate features,
generate signals, or place paper/live trades.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

REQUIRED = {
    "symbol", "side", "timeframe", "market_scope", "signal_time_ms",
    "feature_available_time_ms", "htf_1h_close_time_ms",
    "htf_4h_close_time_ms", "raw_features",
}


def publish(base_url: str, event: dict, username: str, password: str) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("base URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("HTTP is allowed only for localhost; use HTTPS remotely")
    missing = REQUIRED - set(event)
    if missing:
        raise ValueError(f"event missing required fields: {sorted(missing)}")
    if not isinstance(event["raw_features"], dict):
        raise ValueError("raw_features must be an object")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    body = json.dumps(event, allow_nan=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + "/api/ml-shadow/predict",
        data=body,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Do not print request body or credentials; server response is bounded.
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"shadow API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"shadow API connection failed: {exc.reason}") from exc
    return decoded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="API origin, e.g. https://your-render-service.onrender.com")
    parser.add_argument("--input", required=True, type=Path, help="JSONL event file produced by the Vike feature adapter")
    args = parser.parse_args()
    username = os.getenv("TRADER_API_USERNAME", "")
    password = os.getenv("TRADER_API_PASSWORD", "")
    if not username or not password:
        parser.error("TRADER_API_USERNAME and TRADER_API_PASSWORD must be set in the environment")
    if not args.input.is_file():
        parser.error(f"input file does not exist: {args.input}")

    sent = 0
    failed = 0
    with args.input.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("each JSONL row must be an object")
                result = publish(args.base_url, event, username, password)
                print(json.dumps({
                    "line": line_number, "prediction_id": result.get("prediction_id"),
                    "symbol": result.get("symbol"), "side": result.get("side"),
                    "probability": result.get("probability"), "status": result.get("status"),
                }, allow_nan=False))
                sent += 1
            except Exception as exc:
                print(f"line {line_number}: {exc}", file=sys.stderr)
                failed += 1
    print(f"completed: sent={sent}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
