"""Fail-closed, versioned strategy approval artifact loader for live signal emission."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import settings


ARTIFACT_PATH = Path(__file__).with_name("strategy_approval.json")


def current_strategy_config_hash():
    values = {
        "signal_min_adx": settings.signal_min_adx,
        "signal_min_volume_ratio": settings.signal_min_volume_ratio,
        "signal_min_atr_pct": settings.signal_min_atr_pct,
        "signal_min_net_rr": settings.signal_min_net_rr,
        "reward_risk_ratio": settings.reward_risk_ratio,
        "taker_fee_rate": settings.taker_fee_rate,
        "slippage_bps": settings.slippage_bps,
        "higher_timeframe": settings.signal_higher_timeframe,
    }
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_strategy_approval(path=ARTIFACT_PATH):
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(artifact, dict):
        return None
    return artifact


def approval_for_group(exchange, symbol, market_type, timeframe, path=ARTIFACT_PATH):
    """Only exact, current, unexpired approved groups may emit scan signals."""
    artifact = load_strategy_approval(path)
    if not artifact or artifact.get("schema_version") != 1:
        return False, "APPROVAL_ARTIFACT_MISSING_OR_INVALID", None
    if artifact.get("status") != "approved":
        return False, "STRATEGY_NOT_APPROVED", artifact
    groups = artifact.get("approved_groups")
    if not isinstance(groups, list):
        return False, "APPROVAL_GROUPS_INVALID", artifact

    expected_hash = current_strategy_config_hash()
    now = datetime.now(timezone.utc)
    for group in groups:
        if not isinstance(group, dict):
            continue
        if not (
            group.get("exchange") == exchange
            and group.get("symbol") == symbol
            and group.get("market_type") == market_type
            and group.get("timeframe") == timeframe
            and group.get("status") == "approved"
            and group.get("validation_report_id")
            and group.get("strategy_version") == artifact.get("strategy_version")
            and group.get("config_hash") == expected_hash
            and group.get("expires_at")
        ):
            continue
        try:
            expires_at = datetime.fromisoformat(
                str(group["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None or expires_at <= now:
                continue
        except (TypeError, ValueError):
            continue
        return True, None, artifact
    return False, "GROUP_NOT_APPROVED_OR_STALE", artifact
