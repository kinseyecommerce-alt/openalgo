"""Offline unit tests for the RSI mean-reversion strategy's pure signal logic.

The strategy module keeps compute_rsi / crossed_above / crossed_below / decide /
check_price_exit free of network and SDK dependencies, so these tests run
without a broker session, a running server, or market hours.
"""

import math
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies", "examples")
)

from rsi_meanreversion_strategy import (
    ENTER_LONG,
    ENTER_SHORT,
    EXIT,
    FLAT,
    HOLD,
    LONG,
    SHORT,
    check_price_exit,
    compute_rsi,
    crossed_above,
    crossed_below,
    decide,
    update_trailing_stop,
)


class TestComputeRsi:
    def test_warmup_entries_are_nan(self):
        closes = list(range(1, 21))
        rsi = compute_rsi([float(c) for c in closes], period=14)
        assert len(rsi) == 20
        assert all(math.isnan(v) for v in rsi[:14])
        assert all(not math.isnan(v) for v in rsi[14:])

    def test_too_few_candles_all_nan(self):
        rsi = compute_rsi([100.0, 101.0, 102.0], period=14)
        assert all(math.isnan(v) for v in rsi)

    def test_all_gains_is_100(self):
        closes = [100.0 + i for i in range(20)]
        rsi = compute_rsi(closes, period=14)
        assert rsi[-1] == 100.0

    def test_all_losses_is_0(self):
        closes = [100.0 - i for i in range(20)]
        rsi = compute_rsi(closes, period=14)
        assert rsi[-1] == 0.0

    def test_known_wilder_fixture(self):
        # Classic Wilder RSI fixture (Cardwell/StockCharts data set):
        # first RSI(14) value for this series is ~70.53, second ~66.32.
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
        ]
        rsi = compute_rsi(closes, period=14)
        assert abs(rsi[14] - 70.53) < 0.1
        assert abs(rsi[15] - 66.32) < 0.1

    def test_flat_series_rsi_100_by_convention(self):
        # No losses at all -> avg_loss 0 -> RSI 100 (division-by-zero guard).
        closes = [100.0] * 20
        rsi = compute_rsi(closes, period=14)
        assert rsi[-1] == 100.0


class TestCrossings:
    def test_cross_above(self):
        assert crossed_above(29.0, 31.0, 30.0)

    def test_touch_then_rise_counts(self):
        assert crossed_above(30.0, 30.5, 30.0)

    def test_no_cross_above_when_already_above(self):
        assert not crossed_above(31.0, 32.0, 30.0)

    def test_no_cross_above_when_ending_at_level(self):
        assert not crossed_above(29.0, 30.0, 30.0)

    def test_cross_below(self):
        assert crossed_below(71.0, 69.0, 70.0)

    def test_touch_then_fall_counts(self):
        assert crossed_below(70.0, 69.5, 70.0)

    def test_no_cross_below_when_already_below(self):
        assert not crossed_below(69.0, 68.0, 70.0)

    def test_nan_never_crosses(self):
        assert not crossed_above(math.nan, 31.0, 30.0)
        assert not crossed_above(29.0, math.nan, 30.0)
        assert not crossed_below(math.nan, 69.0, 70.0)
        assert not crossed_below(71.0, math.nan, 70.0)


class TestDecide:
    OS, OB = 30.0, 70.0

    def test_flat_long_entry_on_oversold_crossup(self):
        assert decide(FLAT, 29.0, 31.0, self.OS, self.OB, "LONG", False) == ENTER_LONG

    def test_flat_short_entry_on_overbought_crossdown(self):
        assert decide(FLAT, 71.0, 69.0, self.OS, self.OB, "SHORT", False) == ENTER_SHORT

    def test_direction_gate_blocks_short(self):
        assert decide(FLAT, 71.0, 69.0, self.OS, self.OB, "LONG", False) == HOLD

    def test_direction_gate_blocks_long(self):
        assert decide(FLAT, 29.0, 31.0, self.OS, self.OB, "SHORT", False) == HOLD

    def test_both_allows_either_side(self):
        assert decide(FLAT, 29.0, 31.0, self.OS, self.OB, "BOTH", False) == ENTER_LONG
        assert decide(FLAT, 71.0, 69.0, self.OS, self.OB, "BOTH", False) == ENTER_SHORT

    def test_no_entry_after_cutoff(self):
        assert decide(FLAT, 29.0, 31.0, self.OS, self.OB, "LONG", True) == HOLD

    def test_long_exits_on_overbought_crossdown(self):
        assert decide(LONG, 71.0, 69.0, self.OS, self.OB, "LONG", False) == EXIT

    def test_long_holds_otherwise(self):
        assert decide(LONG, 50.0, 55.0, self.OS, self.OB, "LONG", False) == HOLD

    def test_short_exits_on_oversold_crossup(self):
        assert decide(SHORT, 29.0, 31.0, self.OS, self.OB, "SHORT", False) == EXIT

    def test_short_holds_otherwise(self):
        assert decide(SHORT, 50.0, 45.0, self.OS, self.OB, "SHORT", False) == HOLD

    def test_any_open_state_exits_at_cutoff(self):
        assert decide(LONG, 50.0, 55.0, self.OS, self.OB, "LONG", True) == EXIT
        assert decide(SHORT, 50.0, 45.0, self.OS, self.OB, "SHORT", True) == EXIT

    def test_nan_rsi_holds_flat(self):
        assert decide(FLAT, math.nan, math.nan, self.OS, self.OB, "BOTH", False) == HOLD

    def test_flat_hold_when_no_signal(self):
        assert decide(FLAT, 45.0, 50.0, self.OS, self.OB, "BOTH", False) == HOLD


class TestCheckPriceExit:
    def test_long_stoploss(self):
        assert check_price_exit(LONG, 99.0, 99.5, 102.0) == "STOPLOSS"

    def test_long_target(self):
        assert check_price_exit(LONG, 102.5, 99.5, 102.0) == "TARGET"

    def test_long_holds_between(self):
        assert check_price_exit(LONG, 100.5, 99.5, 102.0) is None

    def test_short_stoploss(self):
        assert check_price_exit(SHORT, 101.0, 100.5, 98.0) == "STOPLOSS"

    def test_short_target(self):
        assert check_price_exit(SHORT, 97.5, 100.5, 98.0) == "TARGET"

    def test_short_holds_between(self):
        assert check_price_exit(SHORT, 99.0, 100.5, 98.0) is None

    def test_flat_never_exits(self):
        assert check_price_exit(FLAT, 0.0, 0.0, 0.0) is None

    def test_exact_stoploss_touch_triggers(self):
        assert check_price_exit(LONG, 99.5, 99.5, 102.0) == "STOPLOSS"
        assert check_price_exit(SHORT, 100.5, 100.5, 98.0) == "STOPLOSS"

    def test_infinite_target_never_fires(self):
        # TRAIL mode disables the fixed target by setting it to +/- inf.
        assert check_price_exit(LONG, 1e12, 99.5, math.inf) is None
        assert check_price_exit(SHORT, 1e-9, 100.5, -math.inf) is None


class TestUpdateTrailingStop:
    # LONG at 100, risk (STOPLOSS) 1.0, initial stop 99, breakeven_r 1.5,
    # trail_r 1.0 -- same convention as the four-EMA strategy.

    def test_untouched_below_breakeven_move(self):
        assert update_trailing_stop(LONG, 100.0, 1.0, 101.4, 99.0) == 99.0

    def test_activates_at_breakeven_move(self):
        assert update_trailing_stop(LONG, 100.0, 1.0, 101.5, 99.0) == 100.5

    def test_floors_at_cost(self):
        assert update_trailing_stop(LONG, 100.0, 1.0, 101.5, 99.0, 1.5, 2.0) == 100.0

    def test_ratchets_up_with_extreme(self):
        stop = update_trailing_stop(LONG, 100.0, 1.0, 103.0, 99.0)
        assert stop == 102.0
        assert update_trailing_stop(LONG, 100.0, 1.0, 105.0, stop) == 104.0

    def test_never_loosens(self):
        assert update_trailing_stop(LONG, 100.0, 1.0, 103.0, 104.0) == 104.0

    def test_winner_runs_past_fixed_target(self):
        # At a 6x-risk extreme the trail locks in 5x risk -- far beyond the
        # old fixed 2-rupee TARGET -- and only the giveback ends the trade.
        stop = update_trailing_stop(LONG, 100.0, 1.0, 106.0, 99.0)
        assert stop == 105.0
        assert check_price_exit(LONG, 105.5, stop, math.inf) is None
        assert check_price_exit(LONG, 105.0, stop, math.inf) == "STOPLOSS"

    def test_short_mirror(self):
        assert update_trailing_stop(SHORT, 100.0, 1.0, 98.6, 101.0) == 101.0
        assert update_trailing_stop(SHORT, 100.0, 1.0, 98.5, 101.0) == 99.5
        stop = update_trailing_stop(SHORT, 100.0, 1.0, 96.0, 101.0)
        assert stop == 97.0
        assert update_trailing_stop(SHORT, 100.0, 1.0, 97.5, stop) == 97.0

    def test_short_floors_at_cost(self):
        assert update_trailing_stop(SHORT, 100.0, 1.0, 98.5, 101.0, 1.5, 2.0) == 100.0

    def test_zero_risk_is_noop(self):
        assert update_trailing_stop(LONG, 100.0, 0.0, 150.0, 99.0) == 99.0

    def test_custom_trail_distance(self):
        assert update_trailing_stop(LONG, 100.0, 1.0, 103.0, 99.0, 1.5, 0.5) == 102.5
