"""Offline unit tests for the pre-market screener's pure ranking core.

Covers compute_metrics on hand-built daily bars (known ATR%/ADR%/turnover,
dropping today's partial last bar, too-few -> None) and rank_symbols (liquidity
and price-band filters, top_k, descending sort by each valid sort_key,
deterministic tie-break, None metrics dropped, invalid sort_key raises). No
network, no SDK.
"""

import math
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from screener import VALID_SORT_KEYS, compute_metrics, rank_symbols


def bar(o, h, low, c, v):
    return {"open": o, "high": h, "low": low, "close": c, "volume": v}


def metrics(atr_pct, adr_pct, avg_turnover, avg_volume, last_close):
    return {
        "atr_pct": atr_pct,
        "adr_pct": adr_pct,
        "avg_turnover": avg_turnover,
        "avg_volume": avg_volume,
        "last_close": last_close,
    }


class TestComputeMetrics:
    def _bars(self):
        # c0 padding, c1 & c2 form the lookback=2 window, c3 is today's partial.
        return [
            bar(100, 105, 95, 100, 500),
            bar(105, 110, 100, 105, 1000),
            bar(110, 120, 110, 115, 2000),
            bar(115, 900, 5, 999, 9999),  # partial bar -> must be dropped
        ]

    def test_known_values(self):
        m = compute_metrics(self._bars(), lookback=2)
        assert m is not None
        # last completed close is c2's 115 (partial c3 dropped).
        assert m["last_close"] == 115
        # TR: c1 -> 110-100 = 10; c2 -> max(10, |120-105|=15, |110-105|=5) = 15.
        # ATR = 12.5; ATR% = 12.5 / 115 * 100.
        assert math.isclose(m["atr_pct"], 12.5 / 115 * 100.0, rel_tol=1e-9)
        # ADR% = mean((110-100)/105, (120-110)/115) * 100.
        expected_adr = (10 / 105 + 10 / 115) / 2 * 100.0
        assert math.isclose(m["adr_pct"], expected_adr, rel_tol=1e-9)
        # turnover = mean(105*1000, 115*2000) = 167500; volume = 1500.
        assert math.isclose(m["avg_turnover"], (105 * 1000 + 115 * 2000) / 2, rel_tol=1e-9)
        assert math.isclose(m["avg_volume"], 1500.0, rel_tol=1e-9)

    def test_drops_partial_last_bar(self):
        # The wild c3 must not influence any metric.
        m = compute_metrics(self._bars(), lookback=2)
        assert m["last_close"] == 115
        assert m["avg_volume"] == 1500.0

    def test_too_few_bars_returns_none(self):
        # Need lookback+1 bars (one is the dropped partial).
        assert compute_metrics([bar(1, 2, 1, 1, 1), bar(1, 2, 1, 1, 1)], lookback=2) is None

    def test_empty_returns_none(self):
        assert compute_metrics([], lookback=2) is None
        assert compute_metrics(None, lookback=2) is None

    def test_non_positive_last_close_returns_none(self):
        bars = [bar(1, 2, 1, 1, 10), bar(1, 2, 1, 1, 10), bar(1, 2, 0, 0, 10), bar(1, 2, 1, 1, 10)]
        assert compute_metrics(bars, lookback=2) is None

    def test_zero_lookback_returns_none(self):
        assert compute_metrics(self._bars(), lookback=0) is None


class TestRankSymbols:
    def test_min_turnover_filter_drops_thin_name(self):
        by = {
            "LIQUID": metrics(2.0, 2.0, 1_000_000, 5000, 100),
            "THIN": metrics(9.0, 9.0, 10_000, 50, 100),  # highest vol but illiquid
        }
        out = rank_symbols(by, min_turnover=100_000, min_price=1, max_price=10_000, top_k=10)
        assert out == ["LIQUID"]

    def test_price_band_filter(self):
        by = {
            "CHEAP": metrics(5.0, 5.0, 1_000_000, 5000, 10),
            "OK": metrics(4.0, 4.0, 1_000_000, 5000, 500),
            "PRICEY": metrics(6.0, 6.0, 1_000_000, 5000, 50_000),
        }
        out = rank_symbols(by, min_turnover=0, min_price=50, max_price=10_000, top_k=10)
        assert out == ["OK"]

    def test_top_k_caps_result(self):
        by = {
            "A": metrics(5.0, 5.0, 1_000_000, 5000, 100),
            "B": metrics(4.0, 4.0, 1_000_000, 5000, 100),
            "C": metrics(3.0, 3.0, 1_000_000, 5000, 100),
        }
        out = rank_symbols(by, min_turnover=0, min_price=1, max_price=10_000, top_k=2)
        assert out == ["A", "B"]

    def test_descending_sort_by_each_valid_key(self):
        by = {
            "HI": metrics(9.0, 8.0, 3_000_000, 9000, 100),
            "MID": metrics(5.0, 5.0, 2_000_000, 5000, 100),
            "LO": metrics(1.0, 1.0, 1_000_000, 1000, 100),
        }
        for key in VALID_SORT_KEYS:
            out = rank_symbols(
                by, min_turnover=0, min_price=1, max_price=10_000, top_k=10, sort_key=key
            )
            assert out == ["HI", "MID", "LO"], key

    def test_deterministic_tiebreak_turnover_then_symbol(self):
        # Same atr_pct: higher turnover ranks first.
        by = {
            "BBB": metrics(5.0, 5.0, 2_000_000, 5000, 100),
            "AAA": metrics(5.0, 5.0, 1_000_000, 5000, 100),
            "CCC": metrics(5.0, 5.0, 2_000_000, 5000, 100),  # ties BBB on turnover
        }
        out = rank_symbols(by, min_turnover=0, min_price=1, max_price=10_000, top_k=10)
        # BBB and CCC (turnover 2M) before AAA (1M); BBB before CCC by symbol asc.
        assert out == ["BBB", "CCC", "AAA"]

    def test_none_metrics_dropped(self):
        by = {
            "GOOD": metrics(5.0, 5.0, 1_000_000, 5000, 100),
            "BAD": None,
        }
        out = rank_symbols(by, min_turnover=0, min_price=1, max_price=10_000, top_k=10)
        assert out == ["GOOD"]

    def test_invalid_sort_key_raises(self):
        with pytest.raises(ValueError):
            rank_symbols({}, min_turnover=0, min_price=1, max_price=10, top_k=1, sort_key="sharpe")

    def test_empty_input(self):
        assert rank_symbols({}, min_turnover=0, min_price=1, max_price=10, top_k=5) == []
