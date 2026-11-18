import unittest
from datetime import datetime, timezone
from scripts.collect_hyperliquid_history import coverage, parse_export


class CollectorTests(unittest.TestCase):
    def test_coverage_counts_leading_internal_and_trailing_gaps(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 0, 45, tzinfo=timezone.utc)
        step = 15 * 60_000
        rows = [[int(start.timestamp() * 1000) + step, 1, 1, 1, 1, 1]]
        report = coverage(rows, "15m", start, end)
        self.assertEqual(report["expected_bars"], 3)
        self.assertEqual(report["aligned_bars"], 1)
        self.assertEqual(report["missing_bars_estimate"], 2)
        self.assertEqual(report["gap_segments"], 2)

    def test_empty_ndjson_is_empty_not_error(self):
        self.assertEqual(parse_export("", "BTC", "15m"), [])

    def test_duplicate_candles_rejected(self):
        row = '{"ts":1,"open":1,"high":1,"low":1,"close":1,"volume":1}'
        with self.assertRaises(ValueError):
            parse_export(row + "\\n" + row, "BTC", "15m")


if __name__ == "__main__":
    unittest.main()
