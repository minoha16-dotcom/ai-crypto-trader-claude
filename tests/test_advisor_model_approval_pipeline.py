"""End-to-end approval pipeline test with deterministic mocked Hyperliquid data.

This exercises collector CLI -> raw snapshot -> position reconstruction /
feature dataset -> per-group walk-forward training -> approval JSON and model
artifacts. Network calls and OHLCV are deterministic fixtures; this is not
evidence of real-market profitability or a substitute for the live observation
window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys

from scripts import fetch_hyperliquid_leader_fills as collector
from scripts import prepare_hyperliquid_advisor_dataset as preparer
from scripts import train_advisor_model as trainer


HOUR_MS = 60 * 60 * 1000


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _make_wallet_history(coin: str, count: int, first_signal_ms: int, id_offset: int):
    fills = []
    orders = []
    for index in range(count):
        signal_ms = first_signal_ms + index * HOUR_MS
        close_ms = signal_ms + 15 * 60 * 1000
        is_winner = index % 2 == 0
        exit_price = 103.0 if is_winner else 99.0
        close_oid = id_offset + 200_000 + index
        open_oid = id_offset + 100_000 + index
        pnl = exit_price - 100.0

        fills.append({
            "coin": coin,
            "dir": "Open Long",
            "time": signal_ms,
            "px": "100.0",
            "sz": "1.0",
            "startPosition": "0.0",
            "oid": open_oid,
            "closedPnl": None,
            "fee": "0.0",
            "feeToken": "USDC",
            "tid": f"{coin}-open-{index}",
        })
        fills.append({
            "coin": coin,
            "dir": "Close Long",
            "time": close_ms,
            "px": str(exit_price),
            "sz": "1.0",
            "startPosition": "1.0",
            "oid": close_oid,
            "closedPnl": str(pnl),
            "fee": "0.0",
            "feeToken": "USDC",
            "tid": f"{coin}-close-{index}",
        })
        orders.append({
            "order": {
                "coin": coin,
                "oid": close_oid,
                "side": "A",
                "isTrigger": True,
                "isPositionTpsl": True,
                "reduceOnly": True,
                "triggerPx": str(exit_price),
                "triggerCondition": "Price above" if is_winner else "Price below",
                "orderType": "Take Profit Market" if is_winner else "Stop Market",
            },
            "status": "filled",
            "statusTimestamp": close_ms,
        })
    fills.sort(key=lambda row: (row["time"], row["tid"]))
    return fills, orders


def test_collector_to_independent_group_approval_report_end_to_end(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    data_start = now + timedelta(days=1)
    data_end = data_start + timedelta(days=40)
    data_start_ms = int(data_start.timestamp() * 1000)
    data_end_ms = int(data_end.timestamp() * 1000)
    first_signal_ms = data_start_ms + 120 * HOUR_MS

    btc_address = "0x" + "a" * 40
    eth_address = "0x" + "b" * 40
    btc_fills, btc_orders = _make_wallet_history("BTC", 650, first_signal_ms, 0)
    eth_fills, eth_orders = _make_wallet_history("ETH", 12, first_signal_ms, 1_000_000)
    wallet_data = {
        btc_address: (btc_fills, btc_orders),
        eth_address: (eth_fills, eth_orders),
    }

    metadata = {"as_of": _iso(now), "license": "CC BY 4.0 metadata attribution"}
    raw_board = b'{"leaderboardRows":"deterministic integration fixture"}'
    raw_metadata = json.dumps(metadata, sort_keys=True).encode("utf-8")
    monkeypatch.setattr(
        collector,
        "_load_leaderboard",
        lambda *_args, **_kwargs: (
            raw_board,
            raw_metadata,
            metadata,
            [btc_address, eth_address],
            metadata["as_of"],
        ),
    )

    def fake_fetch_range(address, request_type, _start_ms, _end_ms, *, timeout, pause_seconds):
        fills, _orders = wallet_data[address]
        rows = fills if request_type == "userFillsByTime" else []
        return rows, {
            "requests": 1,
            "rows": len(rows),
            "response_page_truncated": False,
            "history_limit_reached": False,
            "complete": True,
        }

    def fake_twap(_address, _start_ms, _end_ms, *, timeout):
        return [], {
            "rows_returned": 0,
            "rows_in_observation_window": 0,
            "malformed_rows": 0,
            "history_limit_reached": False,
            "complete": True,
            "endpoint": "userTwapSliceFills",
            "limit": 2000,
        }

    def fake_orders(address, *, timeout):
        rows = wallet_data[address][1]
        return rows, {
            "rows": len(rows),
            "history_limit_reached": False,
            "complete_within_documented_limit": True,
        }

    monkeypatch.setattr(collector, "_fetch_time_range", fake_fetch_range)
    monkeypatch.setattr(collector, "_fetch_twap_slice_fills", fake_twap)
    monkeypatch.setattr(collector, "_fetch_historical_orders", fake_orders)

    raw_path = tmp_path / "hyperliquid-raw.json"
    monkeypatch.setattr(sys, "argv", [
        "fetch_hyperliquid_leader_fills.py",
        "--cohort", "top_pnl",
        "--start", _iso(data_start),
        "--end", _iso(data_end),
        "--collector-code-sha", "a" * 40,
        "--wallet-limit", "2",
        "--out", str(raw_path),
    ])
    assert collector.main() == 0
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw_payload["manifest"]["wallets_complete_raw_snapshots"] == 2
    assert all(wallet["training_eligible"] is False for wallet in raw_payload["wallets"])

    class FakeHyperliquid:
        def __init__(self):
            self.markets = {
                "BTC/USDC:USDC": {
                    "id": "BTC", "contract": True, "swap": True,
                    "linear": True, "settle": "USDC", "contractSize": 1.0,
                },
                "ETH/USDC:USDC": {
                    "id": "ETH", "contract": True, "swap": True,
                    "linear": True, "settle": "USDC", "contractSize": 1.0,
                },
            }

        def load_markets(self):
            return self.markets

        def market(self, symbol):
            return self.markets[symbol]

        def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=500):
            assert timeframe == "1h"
            first = (int(since) // HOUR_MS) * HOUR_MS
            rows = []
            for timestamp in range(first, data_end_ms + HOUR_MS, HOUR_MS):
                candle_index = timestamp // HOUR_MS
                close = 100.0 + candle_index * 0.01
                rows.append([
                    timestamp,
                    close - 0.03,
                    close + 0.5,
                    close - 0.5,
                    close,
                    100.0 + (candle_index % 20),
                ])
                if len(rows) >= limit:
                    break
            return rows

        def close(self):
            return None

    monkeypatch.setattr(
        preparer.ccxt,
        "hyperliquid",
        lambda _config: FakeHyperliquid(),
    )

    # Synthetic position-relative features let this test verify data/label
    # plumbing deterministically. Indicator calculations have separate tests.
    def synthetic_features(_candles, entry_timestamp, _timeframe):
        index = (int(entry_timestamp) - first_signal_ms) // HOUR_MS
        is_winner = index % 2 == 0
        return {
            "trend_composite": 0.9 if is_winner else -0.9,
            "rsi14": 70.0 if is_winner else 30.0,
            "adx14": 30.0,
            "volume_ratio": 1.5,
            "atr_pct": 0.8,
        }

    monkeypatch.setattr(preparer, "features_before_entry", synthetic_features)
    prepared_path = tmp_path / "advisor-training-dataset.json"
    monkeypatch.setattr(sys, "argv", [
        "prepare_hyperliquid_advisor_dataset.py",
        "--raw-dataset", str(raw_path),
        "--out", str(prepared_path),
        "--code-sha", "b" * 40,
        "--timeframes", "1h,4h",
    ])
    assert preparer.main() == 0
    prepared_payload = json.loads(prepared_path.read_text(encoding="utf-8"))
    assert len(prepared_payload["groups"]) == 4
    group_counts = {
        (row["symbol"].split("/")[0], row["timeframe"]): len(row["samples"])
        for row in prepared_payload["groups"]
    }
    assert group_counts[("BTC", "1h")] == 650
    assert group_counts[("BTC", "4h")] == 650
    assert group_counts[("ETH", "1h")] == 12
    assert group_counts[("ETH", "4h")] == 12

    out_dir = tmp_path / "advisor-models"
    monkeypatch.setattr(sys, "argv", [
        "train_advisor_model.py",
        "--dataset", str(prepared_path),
        "--out-dir", str(out_dir),
        "--code-sha", "c" * 40,
    ])
    assert trainer.main() == 0

    report_path = out_dir / "advisor-model-approval.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["groups_requested"] == 4
    assert report["groups_approved"] == 2
    assert report["groups_disabled"] == 2
    assert report["model_version"] == "1.2.0"
    assert report["training_code_sha"] == "c" * 40
    assert report["source_provider"]
    assert report["dataset_sha256"]

    btc_reports = [
        row for row in report["groups"]
        if row["group"]["symbol"].startswith("BTC/")
    ]
    eth_reports = [
        row for row in report["groups"]
        if row["group"]["symbol"].startswith("ETH/")
    ]
    assert len(btc_reports) == 2
    assert len(eth_reports) == 2

    for row in btc_reports:
        approval = row["approval"]
        assert approval["approved"] is True
        assert approval["model_status"] == "APPROVED"
        assert approval["rejection_reasons"] == []
        assert approval["metrics"]["min_training_samples_per_fold"] >= 200
        assert approval["metrics"]["walk_forward_folds"] >= 3
        assert approval["metrics"]["oos_signals"] >= 100
        assert approval["metrics"]["oos_samples"] >= 100
        assert approval["metrics"]["top_tercile_samples"] >= 30
        assert approval["metrics"]["uplift"] > 0
        assert approval["metrics"]["uplift_confidence_interval_95"][0] > 0
        assert approval["policy"]["bootstrap_block_hours"] == 24
        assert row["model_artifact"]
        assert (out_dir / row["model_artifact"]).is_file()

    for row in eth_reports:
        approval = row["approval"]
        assert approval["approved"] is False
        assert approval["model_status"] == "DISABLED"
        assert any(
            reason["code"] == "INSUFFICIENT_TRAINING_SAMPLES"
            for reason in approval["rejection_reasons"]
        )
        assert row["model_artifact"] is None

    assert all(
        row["approval"]["provenance"]["backtest_config"]["funding_included"] is True
        for row in btc_reports + eth_reports
    )
