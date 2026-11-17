"""Fail-closed strategy approval artifact loader for live signal emission."""
import json
from pathlib import Path


ARTIFACT_PATH = Path(__file__).with_name("strategy_approval.json")


def load_strategy_approval(path=ARTIFACT_PATH):
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(artifact, dict):
        return None
    return artifact


def approval_for_group(exchange, symbol, market_type, timeframe, path=ARTIFACT_PATH):
    """Only exact, explicitly approved group entries may emit live scan signals."""
    artifact = load_strategy_approval(path)
    if not artifact or artifact.get("schema_version") != 1:
        return False, "APPROVAL_ARTIFACT_MISSING_OR_INVALID", None
    if artifact.get("status") != "approved":
        return False, "STRATEGY_NOT_APPROVED", artifact
    groups = artifact.get("approved_groups")
    if not isinstance(groups, list):
        return False, "APPROVAL_GROUPS_INVALID", artifact
    for group in groups:
        if not isinstance(group, dict):
            continue
        if (
            group.get("exchange") == exchange
            and group.get("symbol") == symbol
            and group.get("market_type") == market_type
            and group.get("timeframe") == timeframe
            and group.get("status") == "approved"
            and group.get("validation_report_id")
            and group.get("strategy_version") == artifact.get("strategy_version")
        ):
            return True, None, artifact
    return False, "GROUP_NOT_APPROVED", artifact
