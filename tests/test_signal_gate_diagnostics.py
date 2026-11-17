from backend import signal_gate_diagnostics as diagnostics


def test_long_gate_diagnostics_counts_each_gate_and_intersections(monkeypatch):
    rows = []
    for index in range(204):
        rows.append([
            index * 3_600_000, 100, 101, 99, 100, 100
        ])

    def fake_analyze(history, signal_overrides=None):
        index = len(history) - 1
        gates = {
            "di_bullish": index % 2 == 0,
            "ema20_above_ema50": index % 3 != 0,
            "price_above_ema200": True,
            "adx_pass": index % 4 != 0,
        }
        return {
            "signal": "LONG" if all(gates.values()) else "WAIT",
            "direction": "bullish" if gates["di_bullish"] else "neutral",
            "long_gate_diagnostics": gates,
        }

    monkeypatch.setattr(diagnostics, "analyze", fake_analyze)
    report = diagnostics.long_gate_diagnostics(
        rows, "1h", start_index=200
    )

    assert report["totals"]["observations"] == 4
    assert report["gates"]["di_bullish"] == {
        "pass": 2, "fail": 2, "not_applicable": 0
    }
    assert report["gates"]["price_above_ema200"]["pass"] == 4
    assert report["gates"]["mtf_bullish"]["not_applicable"] == 4
    assert "di_bullish&mtf_bullish" in report["pairwise_intersections"]
    assert report["totals"]["qualified_long"] == 1
