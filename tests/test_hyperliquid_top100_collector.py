from datetime import date

from scripts import collect_hyperliquid_top100 as collector


def test_wallet_collector_uses_registered_point_in_time_windows():
    assert collector.SELECTION_LABEL_HOUR.isoformat() == "2026-10-16T00:00:00+00:00"
    assert collector.PRIOR_START.isoformat() == "2026-09-16T00:00:00+00:00"
    assert collector.TARGET_START.isoformat() == "2026-10-16T00:00:00+00:00"
    assert collector.TARGET_END.isoformat() == "2026-11-16T00:00:00+00:00"


def test_wallet_collector_date_ranges_are_end_exclusive():
    prior = list(collector.date_range(collector.PRIOR_START, collector.TARGET_START))
    target = list(collector.date_range(collector.TARGET_START, collector.TARGET_END))
    assert len(prior) == 30
    assert prior[0] == date(2026, 9, 16)
    assert prior[-1] == date(2026, 10, 15)
    assert len(target) == 31
    assert target[0] == date(2026, 10, 16)
    assert target[-1] == date(2026, 11, 15)
