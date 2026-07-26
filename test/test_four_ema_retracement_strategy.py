"""Offline unit tests for the four-EMA retracement strategy's pure signal logic.

Covers EMA/RSI math, EMA stacking, focus/confirmation candle detection for
both sides, the RSI 40/60 gates, the 1 percent risk cap, and 1.5R/3R trade
management -- all without network, SDK, or market hours.
"""

import math
import os
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies", "examples"
    ),
)

from four_ema_retracement_strategy import (
    LONG,
    SHORT,
    check_stop,
    compute_ema,
    compute_rsi,
    detect_setup,
    emas_stacked,
    is_green,
    is_red,
    manage_position,
    touched_any_ema,
)


def candle(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close}


class TestComputeEma:
    def test_warmup_is_nan_then_seeded(self):
        values = [float(i) for i in range(1, 11)]
        ema = compute_ema(values, 5)
        assert all(math.isnan(v) for v in ema[:4])
        assert ema[4] == 3.0  # SMA seed of first 5 values

    def test_constant_series_is_constant(self):
        ema = compute_ema([50.0] * 20, 5)
        assert all(abs(v - 50.0) < 1e-9 for v in ema[4:])

    def test_tracks_trend_upward(self):
        values = [float(i) for i in range(1, 31)]
        ema = compute_ema(values, 5)
        assert ema[-1] > ema[-5] > ema[-10]

    def test_too_few_values_all_nan(self):
        assert all(math.isnan(v) for v in compute_ema([1.0, 2.0], 5))


class TestEmasStacked:
    def test_long_stack(self):
        assert emas_stacked([104.0, 103.0, 102.0, 101.0], LONG)

    def test_short_stack(self):
        assert emas_stacked([101.0, 102.0, 103.0, 104.0], SHORT)

    def test_unordered_fails_both(self):
        mixed = [103.0, 104.0, 102.0, 101.0]
        assert not emas_stacked(mixed, LONG)
        assert not emas_stacked(mixed, SHORT)

    def test_nan_fails(self):
        assert not emas_stacked([math.nan, 103.0, 102.0, 101.0], LONG)

    def test_equal_values_fail(self):
        assert not emas_stacked([102.0, 102.0, 101.0, 100.0], LONG)


class TestCandleHelpers:
    def test_red_green(self):
        assert is_red(candle(101, 102, 99, 100))
        assert is_green(candle(100, 102, 99, 101))
        doji = candle(100, 101, 99, 100)
        assert not is_red(doji) and not is_green(doji)

    def test_touch_detection(self):
        c = candle(101, 102, 99, 100)
        assert touched_any_ema(c, [99.5])
        assert touched_any_ema(c, [102.0])  # boundary touch counts
        assert not touched_any_ema(c, [98.0, 103.0])
        assert not touched_any_ema(c, [math.nan])


class TestDetectSetupLong:
    # Uptrend scene at ~1000: stacked EMAs below price, red focus dips to
    # EMA55, green confirmation closes above focus high and above the previous
    # day high, with risk (~6.7 points) inside the 1 percent cap (~10 points).
    FOCUS_EMAS = [997.0, 995.0, 993.0, 991.0]
    CONFIRM_EMAS = [997.3, 995.2, 993.1, 991.05]
    FOCUS = candle(1000.0, 1001.0, 996.8, 997.5)  # red, low touches EMA55 at 997.0
    CONFIRM = candle(997.8, 1003.5, 997.5, 1003.0)  # green, closes above focus high 1001
    PREV_HIGH = 1000.0
    PREV_LOW = 980.0

    def _setup(self, **overrides):
        kwargs = {
            "focus": self.FOCUS,
            "confirm": self.CONFIRM,
            "focus_emas": self.FOCUS_EMAS,
            "confirm_emas": self.CONFIRM_EMAS,
            "rsi_confirm": 52.0,
            "prev_day_high": self.PREV_HIGH,
            "prev_day_low": self.PREV_LOW,
            "side": LONG,
            "sl_buffer_pct": 0.0005,
            "max_risk_pct": 0.01,
        }
        kwargs.update(overrides)
        return detect_setup(**kwargs)

    def test_valid_setup_detected(self):
        setup = self._setup()
        assert setup is not None
        assert setup["side"] == LONG
        assert setup["ref"] == 1003.0
        assert setup["stop"] < self.FOCUS["low"]  # buffer below focus low
        assert 0 < setup["risk"] <= 1003.0 * 0.01  # inside the 1 percent cap

    def test_rejected_below_prev_day_high(self):
        assert self._setup(prev_day_high=1004.0) is None

    def test_rejected_when_emas_not_stacked(self):
        assert self._setup(confirm_emas=[991.0, 995.0, 993.0, 997.0]) is None

    def test_rejected_when_focus_not_red(self):
        green_focus = candle(997.0, 1001.0, 996.8, 1000.5)
        assert self._setup(focus=green_focus) is None

    def test_rejected_when_focus_misses_emas(self):
        assert self._setup(focus_emas=[900.0, 890.0, 880.0, 870.0]) is None

    def test_rejected_when_confirm_not_above_focus_high(self):
        weak_confirm = candle(997.8, 1000.9, 997.5, 1000.8)  # green but below 1001
        assert self._setup(confirm=weak_confirm) is None

    def test_rejected_when_rsi_below_support(self):
        assert self._setup(rsi_confirm=35.0) is None

    def test_rsi_exactly_at_support_accepted(self):
        assert self._setup(rsi_confirm=40.0) is not None

    def test_rejected_when_rsi_nan(self):
        assert self._setup(rsi_confirm=math.nan) is None

    def test_risk_cap_rejects_wide_stop(self):
        # Deep focus low widens risk beyond 1 percent of ~1003 -> rejected
        deep_focus = candle(1000.0, 1001.0, 985.0, 997.5)
        assert self._setup(focus=deep_focus, focus_emas=[990.0, 989.0, 988.0, 987.0]) is None

    def test_risk_cap_relaxed_accepts(self):
        deep_focus = candle(1000.0, 1001.0, 985.0, 997.5)
        setup = self._setup(
            focus=deep_focus, focus_emas=[990.0, 989.0, 988.0, 987.0], max_risk_pct=0.05
        )
        assert setup is not None


class TestDetectSetupShort:
    # Downtrend mirror: stacked EMAs above price, green focus rallies to
    # EMA55, red confirmation closes below focus low and below prev day low.
    FOCUS_EMAS = [101.0, 102.0, 103.0, 104.0]
    CONFIRM_EMAS = [100.8, 101.9, 102.95, 103.98]
    FOCUS = candle(99.5, 101.1, 99.2, 100.8)  # green, high touches EMA55 at 101.0
    CONFIRM = candle(100.7, 100.9, 98.5, 98.8)  # red, closes below focus low 99.2
    PREV_LOW = 100.0
    PREV_HIGH = 105.0

    def _setup(self, **overrides):
        kwargs = {
            "focus": self.FOCUS,
            "confirm": self.CONFIRM,
            "focus_emas": self.FOCUS_EMAS,
            "confirm_emas": self.CONFIRM_EMAS,
            "rsi_confirm": 48.0,
            "prev_day_high": self.PREV_HIGH,
            "prev_day_low": self.PREV_LOW,
            "side": SHORT,
            "sl_buffer_pct": 0.0005,
            "max_risk_pct": 0.03,
        }
        kwargs.update(overrides)
        return detect_setup(**kwargs)

    def test_valid_short_setup(self):
        setup = self._setup()
        assert setup is not None
        assert setup["side"] == SHORT
        assert setup["stop"] > self.FOCUS["high"]
        assert setup["risk"] > 0

    def test_rejected_above_prev_day_low(self):
        assert self._setup(prev_day_low=97.0) is None

    def test_rejected_when_rsi_above_resistance(self):
        assert self._setup(rsi_confirm=65.0) is None

    def test_rsi_exactly_at_resistance_accepted(self):
        assert self._setup(rsi_confirm=60.0) is not None

    def test_rejected_when_focus_not_green(self):
        red_focus = candle(100.8, 101.1, 99.2, 99.5)
        assert self._setup(focus=red_focus) is None

    def test_rejected_when_confirm_not_below_focus_low(self):
        weak_confirm = candle(100.7, 100.9, 99.3, 99.4)
        assert self._setup(confirm=weak_confirm) is None


class TestManagePosition:
    def test_long_breakeven_at_1_5r(self):
        assert manage_position(LONG, 100.0, 1.0, 101.5, False) == "SET_BREAKEVEN"

    def test_long_no_action_below_1_5r(self):
        assert manage_position(LONG, 100.0, 1.0, 101.4, False) is None

    def test_breakeven_fires_once(self):
        assert manage_position(LONG, 100.0, 1.0, 101.6, True) is None

    def test_long_target_at_3r(self):
        assert manage_position(LONG, 100.0, 1.0, 103.0, True) == "TARGET"

    def test_target_beats_breakeven(self):
        assert manage_position(LONG, 100.0, 1.0, 103.2, False) == "TARGET"

    def test_short_breakeven_and_target(self):
        assert manage_position(SHORT, 100.0, 1.0, 98.5, False) == "SET_BREAKEVEN"
        assert manage_position(SHORT, 100.0, 1.0, 97.0, True) == "TARGET"

    def test_zero_risk_never_acts(self):
        assert manage_position(LONG, 100.0, 0.0, 200.0, False) is None

    def test_custom_r_multiples(self):
        assert manage_position(LONG, 100.0, 1.0, 102.0, False, 2.0, 4.0) == "SET_BREAKEVEN"
        assert manage_position(LONG, 100.0, 1.0, 104.0, False, 2.0, 4.0) == "TARGET"


class TestCheckStop:
    def test_long_stop(self):
        assert check_stop(LONG, 98.9, 99.0)
        assert check_stop(LONG, 99.0, 99.0)  # exact touch triggers
        assert not check_stop(LONG, 99.1, 99.0)

    def test_short_stop(self):
        assert check_stop(SHORT, 101.1, 101.0)
        assert check_stop(SHORT, 101.0, 101.0)
        assert not check_stop(SHORT, 100.9, 101.0)


class TestRsiParity:
    def test_wilder_fixture(self):
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
        ]
        rsi = compute_rsi(closes, period=14)
        assert abs(rsi[14] - 70.53) < 0.1
        assert abs(rsi[15] - 66.32) < 0.1
