from pathlib import Path
import re
import textwrap


def test_free_position_monitor_embedded_python_is_valid_and_reports_partial_failures():
    workflow = Path(".github/workflows/free-position-monitor.yml").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"python - <<'PY'\n(?P<script>.*?)\n\s+PY",
        workflow,
        flags=re.DOTALL,
    )
    assert match is not None, "Expected the inline monitor script in the workflow"

    script = textwrap.dedent(match.group("script"))
    compile(script, "free-position-monitor-inline.py", "exec")
    assert "monitor_open_positions_once(detailed=True)" in script
    assert 'if report["failed"]' in script
    assert "TRADER_DATABASE_URL" in script


def test_supabase_probe_is_explicitly_read_only():
    probe = Path("tests/test_supabase_readonly_probe.py").read_text(encoding="utf-8")
    assert "default_transaction_read_only=on" in probe
    assert "TRADER_DATABASE_URL" in probe
    assert "to_regclass('public.trades')" in probe


def test_free_position_monitor_is_disabled_until_explicitly_enabled():
    workflow = Path(".github/workflows/free-position-monitor.yml").read_text(
        encoding="utf-8"
    )
    assert "vars.ENABLE_PAPER_POSITION_MONITOR == 'true'" in workflow
