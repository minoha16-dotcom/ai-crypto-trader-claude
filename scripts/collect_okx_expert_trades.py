"""Collect public OKX Copy Trading lead-trader positions for research.

The collector is deliberately prospective by default. A trader's first observed
qualification time is persisted; only positions opened after that observation
can enter the prospective expert-outcome training set. Older history can be
stored for descriptive research, but is permanently marked research_only.

OKX public pnlRatio is an ROI-style outcome, not risk-multiple R. This dataset
must never be merged into the strategy's net-R training labels.
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://www.okx.com"
RANK_ENDPOINT = "/api/v5/copytrading/public-lead-traders"
HISTORY_ENDPOINT = "/api/v5/copytrading/public-subpositions-history"
CANDLE_ENDPOINT = "/api/v5/market/history-candles"
RANK_RATE_PAUSE_SECONDS = 0.45
MARKET_RATE_PAUSE_SECONDS = 0.12
TIMEFRAME = "1H"
TIMEFRAME_MS = 60 * 60 * 1000
DB_SCHEMA_VERSION = 1


def _utc_now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _finite_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed == parsed and abs(parsed) != float("inf") else default


def _request_json(endpoint, params, timeout=20):
    pause = MARKET_RATE_PAUSE_SECONDS if endpoint == CANDLE_ENDPOINT else RANK_RATE_PAUSE_SECONDS
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    url = f"{BASE_URL}{endpoint}?{query}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ai-crypto-trader-research/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        time.sleep(pause)
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        message = payload.get("msg", "invalid response") if isinstance(payload, dict) else "invalid response"
        raise RuntimeError(f"OKX public API error: {message}")
    return payload


def init_database(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS leaders (
            unique_code TEXT PRIMARY KEY,
            nickname TEXT,
            first_qualified_at_ms INTEGER NOT NULL,
            first_seen_at_ms INTEGER NOT NULL,
            last_seen_at_ms INTEGER NOT NULL,
            lead_days INTEGER,
            pnl REAL,
            pnl_ratio REAL,
            win_ratio REAL,
            trader_instruments_json TEXT NOT NULL,
            latest_rank_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leader_observations (
            unique_code TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            data_version TEXT,
            lead_days INTEGER,
            pnl REAL,
            pnl_ratio REAL,
            win_ratio REAL,
            rank_json TEXT NOT NULL,
            PRIMARY KEY (unique_code, observed_at_ms)
        );
        CREATE TABLE IF NOT EXISTS lead_positions (
            unique_code TEXT NOT NULL,
            sub_pos_id TEXT NOT NULL,
            inst_id TEXT NOT NULL,
            inst_type TEXT NOT NULL,
            pos_side TEXT,
            leverage REAL,
            margin_mode TEXT,
            open_time_ms INTEGER NOT NULL,
            close_time_ms INTEGER NOT NULL,
            open_avg_px REAL,
            close_avg_px REAL,
            position_size REAL,
            pnl REAL,
            pnl_ratio REAL,
            currency TEXT,
            first_qualified_at_ms INTEGER NOT NULL,
            prospective_eligible INTEGER NOT NULL DEFAULT 0,
            research_only INTEGER NOT NULL DEFAULT 1,
            features_json TEXT,
            feature_error TEXT,
            raw_json TEXT NOT NULL,
            collected_at_ms INTEGER NOT NULL,
            PRIMARY KEY (unique_code, sub_pos_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lead_positions_group
            ON lead_positions(inst_id, inst_type, open_time_ms);
        CREATE INDEX IF NOT EXISTS idx_lead_positions_eligible
            ON lead_positions(prospective_eligible, features_json);
        CREATE TABLE IF NOT EXISTS collection_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER,
            leaders_seen INTEGER NOT NULL DEFAULT 0,
            leaders_qualified INTEGER NOT NULL DEFAULT 0,
            positions_seen INTEGER NOT NULL DEFAULT 0,
            prospective_positions INTEGER NOT NULL DEFAULT 0,
            research_only_positions INTEGER NOT NULL DEFAULT 0,
            errors_json TEXT NOT NULL DEFAULT '[]'
        );
        PRAGMA user_version = 1;
        """
    )
    connection.commit()
    return connection


def _rank_payload(payload):
    blocks = payload.get("data") or []
    if not blocks:
        return None, [], 0
    block = blocks[0]
    return (
        str(block.get("dataVer") or ""),
        block.get("ranks") or [],
        int(block.get("totalPage") or 1),
    )


def _is_qualified(rank, *, min_lead_days=90, min_win_ratio=0.55):
    lead_days = _finite_float(rank.get("leadDays"), 0)
    pnl = _finite_float(rank.get("pnl"), 0)
    pnl_ratio = _finite_float(rank.get("pnlRatio"), 0)
    win_ratio = _finite_float(rank.get("winRatio"), 0)
    return (
        bool(rank.get("uniqueCode"))
        and lead_days >= min_lead_days
        and pnl > 0
        and pnl_ratio > 0
        and win_ratio >= min_win_ratio
    )


def _upsert_qualified_leader(connection, rank, observed_at_ms, data_version):
    unique_code = str(rank["uniqueCode"])
    existing = connection.execute(
        "SELECT first_qualified_at_ms, first_seen_at_ms FROM leaders WHERE unique_code=?",
        (unique_code,),
    ).fetchone()
    first_qualified = (
        int(existing["first_qualified_at_ms"])
        if existing else int(observed_at_ms)
    )
    first_seen = int(existing["first_seen_at_ms"]) if existing else int(observed_at_ms)
    instruments = rank.get("traderInsts") or []
    connection.execute(
        """
        INSERT INTO leaders (
            unique_code, nickname, first_qualified_at_ms, first_seen_at_ms,
            last_seen_at_ms, lead_days, pnl, pnl_ratio, win_ratio,
            trader_instruments_json, latest_rank_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(unique_code) DO UPDATE SET
            nickname=excluded.nickname,
            last_seen_at_ms=excluded.last_seen_at_ms,
            lead_days=excluded.lead_days,
            pnl=excluded.pnl,
            pnl_ratio=excluded.pnl_ratio,
            win_ratio=excluded.win_ratio,
            trader_instruments_json=excluded.trader_instruments_json,
            latest_rank_json=excluded.latest_rank_json
        """,
        (
            unique_code,
            rank.get("nickName"),
            first_qualified,
            first_seen,
            int(observed_at_ms),
            int(_finite_float(rank.get("leadDays"), 0)),
            _finite_float(rank.get("pnl"), 0),
            _finite_float(rank.get("pnlRatio"), 0),
            _finite_float(rank.get("winRatio"), 0),
            json.dumps(instruments, separators=(",", ":")),
            json.dumps(rank, separators=(",", ":"), ensure_ascii=False),
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO leader_observations (
            unique_code, observed_at_ms, data_version, lead_days,
            pnl, pnl_ratio, win_ratio, rank_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            unique_code,
            int(observed_at_ms),
            data_version,
            int(_finite_float(rank.get("leadDays"), 0)),
            _finite_float(rank.get("pnl"), 0),
            _finite_float(rank.get("pnlRatio"), 0),
            _finite_float(rank.get("winRatio"), 0),
            json.dumps(rank, separators=(",", ":"), ensure_ascii=False),
        ),
    )
    return first_qualified


def _position_side(position):
    side = str(position.get("posSide") or "").lower()
    if side in ("long", "short"):
        return side.upper()
    size = _finite_float(position.get("subPos"), 0)
    return "LONG" if size > 0 else "SHORT" if size < 0 else None


def _features_at_open(position, fetch_json=_request_json):
    """Compute features only from fully closed 1H candles before the leader entry."""
    open_time = int(position["openTime"])
    payload = fetch_json(
        CANDLE_ENDPOINT,
        {
            "instId": position["instId"],
            "bar": TIMEFRAME,
            "after": open_time,
            "limit": 100,
        },
    )
    candles = []
    for row in payload.get("data") or []:
        if len(row) < 9:
            continue
        timestamp = int(row[0])
        if timestamp + TIMEFRAME_MS > open_time or str(row[8]) != "1":
            continue
        candles.append([
            timestamp,
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[5]),
        ])
    candles.sort(key=lambda row: row[0])
    if len(candles) < 50:
        raise ValueError(
            f"need 50 closed 1H candles before leader entry; received {len(candles)}"
        )

    # The feature code is shared with the strategy and uses only these past bars.
    from backend.indicators import analyze
    from backend.advisor_model import features_from_signal

    metrics = analyze(candles)
    side = _position_side(position)
    if side is None:
        raise ValueError("leader position direction is unavailable")
    vector = features_from_signal({
        "side": side,
        "signal_features": metrics,
    })
    return {
        "feature_schema_version": "advisor-r-features-1",
        "timeframe": TIMEFRAME,
        "features": list(vector),
        "feature_names": [
            "directional_composite_trend",
            "directional_rsi",
            "adx_strength",
            "log_volume_ratio",
            "log_atr_percent",
        ],
        "as_of_signal_time_ms": open_time,
        "last_feature_candle_open_ms": int(candles[-1][0]),
        "feature_candle_count": len(candles),
    }


def _store_position(
    connection,
    unique_code,
    first_qualified_at_ms,
    position,
    collected_at_ms,
    *,
    allow_retrospective_research=False,
    enrich_features=True,
    fetch_json=_request_json,
):
    open_time = int(position.get("openTime") or 0)
    close_time = int(position.get("closeTime") or 0)
    prospective = open_time >= int(first_qualified_at_ms)
    research_only = not prospective
    features_json = None
    feature_error = None
    if prospective and enrich_features:
        try:
            features_json = json.dumps(
                _features_at_open(position, fetch_json=fetch_json),
                separators=(",", ":"),
            )
        except Exception as error:
            feature_error = f"{type(error).__name__}: {error}"

    # Historical positions can be retained for descriptive research when the
    # explicit flag is enabled, but they are never made eligible for training.
    if research_only and not allow_retrospective_research:
        prospective_eligible = 0
    else:
        prospective_eligible = int(prospective and bool(features_json))

    connection.execute(
        """
        INSERT INTO lead_positions (
            unique_code, sub_pos_id, inst_id, inst_type, pos_side,
            leverage, margin_mode, open_time_ms, close_time_ms,
            open_avg_px, close_avg_px, position_size, pnl, pnl_ratio,
            currency, first_qualified_at_ms, prospective_eligible,
            research_only, features_json, feature_error, raw_json, collected_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(unique_code, sub_pos_id) DO UPDATE SET
            features_json=COALESCE(excluded.features_json, lead_positions.features_json),
            feature_error=excluded.feature_error,
            prospective_eligible=MAX(lead_positions.prospective_eligible, excluded.prospective_eligible),
            research_only=MIN(lead_positions.research_only, excluded.research_only),
            collected_at_ms=excluded.collected_at_ms
        """,
        (
            str(unique_code),
            str(position.get("subPosId") or ""),
            str(position.get("instId") or ""),
            str(position.get("instType") or "SWAP"),
            _position_side(position),
            _finite_float(position.get("lever")),
            position.get("mgnMode"),
            open_time,
            close_time,
            _finite_float(position.get("openAvgPx")),
            _finite_float(position.get("closeAvgPx")),
            _finite_float(position.get("subPos")),
            _finite_float(position.get("pnl")),
            _finite_float(position.get("pnlRatio")),
            position.get("ccy"),
            int(first_qualified_at_ms),
            prospective_eligible,
            int(research_only),
            features_json,
            feature_error,
            json.dumps(position, separators=(",", ":"), ensure_ascii=False),
            int(collected_at_ms),
        ),
    )
    return prospective, bool(features_json), feature_error


def collect_okx_expert_trades(
    db_path,
    *,
    fetch_json=_request_json,
    now_ms=None,
    inst_type="SWAP",
    rank_pages=3,
    history_pages=3,
    max_leaders=30,
    min_lead_days=90,
    min_win_ratio=0.55,
    allow_retrospective_research=False,
    enrich_features=True,
):
    """Collect qualified leader histories while retaining prospective eligibility."""
    observed_at = int(now_ms if now_ms is not None else _utc_now_ms())
    connection = init_database(db_path)
    run_id = connection.execute(
        "INSERT INTO collection_runs(started_at_ms) VALUES (?)",
        (observed_at,),
    ).lastrowid
    errors = []
    seen_leaders = 0
    qualified_leaders = 0
    positions_seen = 0
    prospective_positions = 0
    research_only_positions = 0
    try:
        first_payload = fetch_json(
            RANK_ENDPOINT,
            {
                "instType": inst_type,
                "sortType": "pnl_ratio",
                "minLeadDays": "3" if min_lead_days >= 90 else "2",
                "limit": 20,
                "page": 1,
            },
        )
        data_version, first_ranks, total_pages = _rank_payload(first_payload)
        pages_to_fetch = max(1, min(int(rank_pages), int(total_pages)))
        candidates = list(first_ranks)
        for page in range(2, pages_to_fetch + 1):
            payload = fetch_json(
                RANK_ENDPOINT,
                {
                    "instType": inst_type,
                    "sortType": "pnl_ratio",
                    "minLeadDays": "3" if min_lead_days >= 90 else "2",
                    "dataVer": data_version,
                    "limit": 20,
                    "page": page,
                },
            )
            _, page_ranks, _ = _rank_payload(payload)
            candidates.extend(page_ranks)

        seen_leaders = len(candidates)
        qualified = [
            rank for rank in candidates
            if _is_qualified(
                rank,
                min_lead_days=min_lead_days,
                min_win_ratio=min_win_ratio,
            )
        ][:max_leaders]
        qualified_leaders = len(qualified)

        for rank in qualified:
            unique_code = str(rank["uniqueCode"])
            first_qualified_at = _upsert_qualified_leader(
                connection, rank, observed_at, data_version
            )
            after = None
            for _page in range(max(1, int(history_pages))):
                params = {
                    "instType": inst_type,
                    "uniqueCode": unique_code,
                    "limit": 100,
                }
                if after:
                    params["after"] = after
                try:
                    payload = fetch_json(HISTORY_ENDPOINT, params)
                except Exception as error:
                    errors.append({
                        "unique_code": unique_code,
                        "endpoint": HISTORY_ENDPOINT,
                        "error": f"{type(error).__name__}: {error}",
                    })
                    break
                positions = payload.get("data") or []
                if not positions:
                    break
                for position in positions:
                    positions_seen += 1
                    is_prospective, has_features, feature_error = _store_position(
                        connection,
                        unique_code,
                        first_qualified_at,
                        position,
                        observed_at,
                        allow_retrospective_research=allow_retrospective_research,
                        enrich_features=enrich_features,
                        fetch_json=fetch_json,
                    )
                    if is_prospective:
                        prospective_positions += 1
                    else:
                        research_only_positions += 1
                    if feature_error:
                        errors.append({
                            "unique_code": unique_code,
                            "sub_pos_id": str(position.get("subPosId") or ""),
                            "error": feature_error,
                        })
                after = str(positions[-1].get("subPosId") or "")
                connection.commit()
                # If the next page would be entirely older than the cohort's
                # first qualification, no further prospective labels can enter.
                if (
                    not allow_retrospective_research
                    and all(int(item.get("openTime") or 0) < first_qualified_at for item in positions)
                ):
                    break
        connection.commit()
        connection.execute(
            """
            UPDATE collection_runs SET completed_at_ms=?, leaders_seen=?,
                leaders_qualified=?, positions_seen=?, prospective_positions=?,
                research_only_positions=?, errors_json=? WHERE run_id=?
            """,
            (
                _utc_now_ms(),
                seen_leaders,
                qualified_leaders,
                positions_seen,
                prospective_positions,
                research_only_positions,
                json.dumps(errors, separators=(",", ":"), ensure_ascii=False),
                run_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    return {
        "run_id": run_id,
        "leaders_seen": seen_leaders,
        "leaders_qualified": qualified_leaders,
        "positions_seen": positions_seen,
        "prospective_positions": prospective_positions,
        "research_only_positions": research_only_positions,
        "errors": errors,
        "db_path": str(db_path),
        "source": "OKX Copy Trading public lead trader rankings and closed lead position history",
        "label_warning": "pnlRatio is a leader ROI-style label, not true risk-multiple R.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/okx-expert-trades.sqlite3")
    parser.add_argument("--rank-pages", type=int, default=3)
    parser.add_argument("--history-pages", type=int, default=3)
    parser.add_argument("--max-leaders", type=int, default=30)
    parser.add_argument("--min-lead-days", type=int, default=90)
    parser.add_argument("--min-win-ratio", type=float, default=0.55)
    parser.add_argument("--allow-retrospective-research", action="store_true")
    parser.add_argument("--skip-feature-enrichment", action="store_true")
    args = parser.parse_args()
    result = collect_okx_expert_trades(
        args.db_path,
        rank_pages=args.rank_pages,
        history_pages=args.history_pages,
        max_leaders=args.max_leaders,
        min_lead_days=args.min_lead_days,
        min_win_ratio=args.min_win_ratio,
        allow_retrospective_research=args.allow_retrospective_research,
        enrich_features=not args.skip_feature_enrichment,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
