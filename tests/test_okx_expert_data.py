import json
import sqlite3

from scripts.collect_okx_expert_trades import (
    CANDLE_ENDPOINT,
    HISTORY_ENDPOINT,
    RANK_ENDPOINT,
    collect_okx_expert_trades,
)
from scripts.train_okx_expert_model import train_okx_expert_models


def _position(sub_pos_id, inst_id, open_time, close_time, pnl, pnl_ratio):
    return {
        "ccy": "USDT",
        "closeAvgPx": "105",
        "closeTime": str(close_time),
        "instId": inst_id,
        "instType": "SWAP",
        "lever": "3",
        "margin": "100",
        "mgnMode": "isolated",
        "openAvgPx": "100",
        "openTime": str(open_time),
        "pnl": str(pnl),
        "pnlRatio": str(pnl_ratio),
        "posSide": "long",
        "subPos": "1",
        "subPosId": str(sub_pos_id),
        "uniqueCode": "LEADER0000000001",
    }


def _rank_response():
    return {
        "code": "0",
        "data": [{
            "dataVer": "20261116090000",
            "totalPage": "1",
            "ranks": [{
                "uniqueCode": "LEADER0000000001",
                "nickName": "Public Leader",
                "leadDays": "120",
                "pnl": "5000",
                "pnlRatio": "0.35",
                "winRatio": "0.65",
                "traderInsts": ["BTC-USDT-SWAP"],
            }],
        }],
    }


def test_okx_collector_stores_only_trades_opened_after_first_qualification(tmp_path):
    db_path = tmp_path / "okx.sqlite3"
    base = 1_704_067_200_000
    old_trade = _position(
        "100", "BTC-USDT-SWAP", base, base + 3_600_000, 5, 0.05
    )
    prospective_trade = _position(
        "101", "BTC-USDT-SWAP", base + 60 * 3_600_000,
        base + 65 * 3_600_000, 10, 0.10
    )

    def first_fetch(endpoint, params):
        if endpoint == RANK_ENDPOINT:
            return _rank_response()
        if endpoint == HISTORY_ENDPOINT:
            return {"code": "0", "data": [old_trade]}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    first = collect_okx_expert_trades(
        db_path,
        fetch_json=first_fetch,
        now_ms=base + 1_000,
        rank_pages=1,
        history_pages=1,
        max_leaders=1,
        enrich_features=False,
    )
    assert first["leaders_qualified"] == 1
    assert first["prospective_positions"] == 0
    assert first["research_only_positions"] == 1

    def second_fetch(endpoint, params):
        if endpoint == RANK_ENDPOINT:
            return _rank_response()
        if endpoint == HISTORY_ENDPOINT:
            return {"code": "0", "data": [prospective_trade, old_trade]}
        if endpoint == CANDLE_ENDPOINT:
            rows = []
            for index in range(60):
                ts = base + index * 3_600_000
                close = 100.0 + index * 0.1
                rows.append([
                    str(ts), str(close - 0.1), str(close + 1),
                    str(close - 1), str(close), "1000", "1000", "1000", "1",
                ])
            return {"code": "0", "data": list(reversed(rows))}
        raise AssertionError(f"unexpected endpoint {endpoint}")

    second = collect_okx_expert_trades(
        db_path,
        fetch_json=second_fetch,
        now_ms=base + 70 * 3_600_000,
        rank_pages=1,
        history_pages=1,
        max_leaders=1,
        enrich_features=True,
    )
    assert second["prospective_positions"] == 1

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT sub_pos_id, prospective_eligible, research_only, pnl_ratio, features_json "
            "FROM lead_positions ORDER BY sub_pos_id"
        ).fetchall()
    finally:
        connection.close()

    old_row, new_row = rows
    assert old_row[0] == "100"
    assert old_row[1] == 0
    assert old_row[2] == 1
    assert new_row[0] == "101"
    assert new_row[1] == 1
    assert new_row[2] == 0
    assert json.loads(new_row[4])["feature_schema_version"] == "advisor-r-features-1"


def test_okx_expert_trainer_keeps_pnl_ratio_target_separate_from_r(tmp_path):
    db_path = tmp_path / "expert-training.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE lead_positions (
            unique_code TEXT, sub_pos_id TEXT, inst_id TEXT, inst_type TEXT,
            open_time_ms INTEGER, close_time_ms INTEGER, pnl REAL,
            pnl_ratio REAL, features_json TEXT, prospective_eligible INTEGER,
            research_only INTEGER
        );
        """
    )
    base = 1_704_067_200_000
    for index in range(250):
        x = -1.0 + 2.0 * index / 249
        features = {
            "timeframe": "1H",
            "features": [x, 0.1, 0.2, 0.3, 0.4],
        }
        open_time = base + index * 3_600_000
        connection.execute(
            """
            INSERT INTO lead_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            """,
            (
                "LEADER0000000001",
                str(index),
                "BTC-USDT-SWAP",
                "SWAP",
                open_time,
                open_time + 1_800_000,
                100 * x,
                0.1 * x,
                json.dumps(features),
            ),
        )
    connection.commit()
    connection.close()

    report = train_okx_expert_models(
        db_path,
        output_path=tmp_path / "expert-report.json",
        min_training_samples=200,
        min_oos_samples=50,
        bootstrap_replicates=100,
    )
    group = report["groups"][0]
    assert group["model_metadata"]["target"] == "leader_pnl_ratio_not_R"
    assert group["model_metadata"]["data_source"]["provider"].startswith("OKX Copy Trading")
    assert group["model_metadata"]["dataset_sha256"]
    assert group["training_samples"] == 200
    assert group["oos_samples"] == 50
    assert group["approval_status"] == "RESEARCH_ONLY"
    assert group["model"]["label_source"] == "public_okx_copy_trading_history"
