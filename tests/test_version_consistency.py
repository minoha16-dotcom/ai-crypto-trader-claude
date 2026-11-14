import json
from pathlib import Path

from backend.version import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_application_and_claude_plugin_versions_share_canonical_version():
    version_file = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    plugin = json.loads(
        (ROOT / "plugins/ai-crypto-trader/.claude-plugin/plugin.json")
        .read_text(encoding="utf-8")
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    marketplace = json.loads(
        (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )

    assert APP_VERSION == version_file
    assert f"Release: {version_file}" in readme
    assert plugin["version"] == version_file
    assert marketplace["metadata"]["version"] == version_file
    assert marketplace["plugins"][0]["version"] == version_file
