"""Offline unit tests for the 20-variant intraday strategy engine.

Covers three layers of strategies/scripts/variant_intraday_strategy.py without
network, SDK, or market hours:

  1. The pure indicator library (EMA/RSI/VWAP/ATR/Supertrend/Bollinger/MACD/
     Stochastic/Donchian/ROC/Heikin-Ashi/pivots): correctness, nan-padded
     warmups, and no exceptions on short input.
  2. Registry invariants and the shared entry wrapper: exactly 20 variants,
     the entry contract (ref/stop/risk, correct stop side, the 1 percent risk
     cap MAX_RISK_PCT), None (never a raise) on insufficient data.
  3. Per-family signal scenes: hand-built candle series proving eight
     representative variants both TRIGGER on a valid setup and REJECT a nearby
     non-setup (four of them mirrored for SHORT).

Candles are dicts with open/high/low/close/volume/ts; every signal evaluates
the LAST closed candle. Timestamps are real datetimes so the date-boundary /
opening-range logic exercises correctly.
"""

import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies", "scripts"
    ),
)

# The reference (validated) strategy, for EMA/RSI parity checks.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies", "examples"
    ),
)

import pytest  # noqa: E402
from four_ema_retracement_strategy import compute_ema as ref_compute_ema  # noqa: E402
from variant_intraday_strategy import (  # noqa: E402
    DEFAULT_VARIANT,
    LONG,
    MAX_RISK_PCT,
    SHORT,
    VARIANTS,
    classic_pivots,
    compute_atr,
    compute_bollinger,
    compute_donchian,
    compute_ema,
    compute_macd,
    compute_roc,
    compute_rsi,
    compute_sma,
    compute_stochastic,
    compute_supertrend,
    compute_vwap,
    heikin_ashi,
    resolve_variant_key,
)

# The 20 keys the module documents / registers.
EXPECTED_KEYS = {
    "ema_ribbon_trend",
    "vwap_breakout",
    "vwap_reversion",
    "orb_breakout",
    "supertrend_follow",
    "bollinger_squeeze",
    "bollinger_reversion",
    "macd_momentum",
    "donchian_breakout",
    "stochastic_reversal",
    "atr_channel_ride",
    "prev_day_level_fade",
    "momentum_roc",
    "heikin_ashi_trend",
    "volume_spike_breakout",
    "inside_bar_breakout",
    "engulfing_at_ema",
    "pivot_bounce",
    "gap_go",
    "triple_ema_cross",
}

BASE = datetime(2024, 5, 20, 9, 15)


# ------------------------------- helpers -------------------------------


def candle(open_, high, low, close, volume=1000.0, ts=None):
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "ts": ts}


def ts_at(i, minutes=3):
    """Timestamp of the i-th 3-minute candle in the session."""
    return BASE + timedelta(minutes=minutes * i)


def flat_series(n, price=100.0, spread=0.1, volume=1000.0, start_idx=0):
    """n identical-ish candles hugging `price` (no signal anywhere)."""
    return [
        candle(price, price + spread, price - spread, price, volume, ts_at(start_idx + i))
        for i in range(n)
    ]


def entry_of(key):
    return VARIANTS[key]["entry"]


def assert_valid_setup(setup, side, ref_hint=None):
    """A returned setup must satisfy the shared entry contract."""
    assert setup is not None
    assert setup["side"] == side
    ref, stop, risk = setup["ref"], setup["stop"], setup["risk"]
    assert ref > 0
    assert stop > 0
    assert risk > 0
    # risk must not exceed the hard 1 percent cap of the reference price.
    assert risk <= ref * MAX_RISK_PCT + 1e-9
    # stop on the correct side of the reference.
    if side == LONG:
        assert stop < ref
    else:
        assert stop > ref
    # risk is exactly the ref-to-stop distance (rounded).
    expected_risk = (ref - stop) if side == LONG else (stop - ref)
    assert abs(risk - round(expected_risk, 2)) < 1e-9
    if ref_hint is not None:
        assert abs(ref - ref_hint) < 1e-9


# ===============================================================================
# 1. PURE INDICATOR LIBRARY
# ===============================================================================


class TestComputeEma:
    def test_sma_seed_and_nan_warmup(self):
        values = [float(i) for i in range(1, 11)]
        ema = compute_ema(values, 5)
        assert all(math.isnan(v) for v in ema[:4])  # warmup nan
        assert ema[4] == 3.0  # SMA seed of first 5 values (1..5)

    def test_matches_reference_strategy_ema(self):
        # The engine's EMA must be byte-for-byte the validated reference EMA.
        values = [100.0 + math.sin(i / 3.0) * 5 + i * 0.2 for i in range(60)]
        mine = compute_ema(values, 21)
        theirs = ref_compute_ema(values, 21)
        assert len(mine) == len(theirs)
        for a, b in zip(mine, theirs, strict=True):
            assert (math.isnan(a) and math.isnan(b)) or abs(a - b) < 1e-12

    def test_constant_series_is_constant(self):
        ema = compute_ema([50.0] * 20, 5)
        assert all(abs(v - 50.0) < 1e-9 for v in ema[4:])

    def test_too_few_values_all_nan(self):
        assert all(math.isnan(v) for v in compute_ema([1.0, 2.0], 5))


class TestComputeRsi:
    def test_wilder_fixture(self):
        # Same Wilder fixture used by test_four_ema_retracement_strategy.py.
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
        ]
        rsi = compute_rsi(closes, period=14)
        assert all(math.isnan(v) for v in rsi[:14])  # first `period` are nan
        assert abs(rsi[14] - 70.53) < 0.1
        assert abs(rsi[15] - 66.32) < 0.1

    def test_all_gains_is_100(self):
        rsi = compute_rsi([float(i) for i in range(1, 30)], 14)
        assert abs(rsi[-1] - 100.0) < 1e-9

    def test_short_input_all_nan(self):
        assert all(math.isnan(v) for v in compute_rsi([1.0, 2.0, 3.0], 14))


class TestComputeVwap:
    def test_hand_computed_with_day_reset(self):
        # Day 1: two candles; Day 2: one candle -> VWAP must reset at the
        # date boundary and not carry day-1 volume.
        d1 = datetime(2024, 5, 20, 9, 15)
        d2 = datetime(2024, 5, 21, 9, 15)
        candles = [
            candle(9, 10, 8, 9, volume=100, ts=d1),  # tp=9
            candle(11, 12, 10, 11, volume=100, ts=d1 + timedelta(minutes=3)),  # tp=11
            candle(19, 20, 18, 19, volume=50, ts=d2),  # tp=19, new session
        ]
        vwap = compute_vwap(candles)
        assert abs(vwap[0] - 9.0) < 1e-9  # 900/100
        assert abs(vwap[1] - 10.0) < 1e-9  # (900+1100)/200
        assert abs(vwap[2] - 19.0) < 1e-9  # reset: 950/50, day-1 dropped

    def test_zero_volume_is_nan(self):
        c = [candle(9, 10, 8, 9, volume=0, ts=BASE)]
        assert math.isnan(compute_vwap(c)[0])

    def test_empty_no_exception(self):
        assert compute_vwap([]) == []


class TestComputeAtr:
    def test_seed_is_mean_true_range(self):
        highs = [10, 11, 12, 13, 14]
        lows = [8, 9, 10, 11, 12]
        closes = [9, 10, 11, 12, 13]
        atr = compute_atr(highs, lows, closes, period=3)
        assert all(math.isnan(v) for v in atr[:2])  # period-1 warmup
        # TR[0]=2, TR[1]=max(2,|11-9|,|9-9|)=2, TR[2]=2 -> seed = 2.0
        assert abs(atr[2] - 2.0) < 1e-9

    def test_positive_and_no_exception_on_short(self):
        atr = compute_atr([10, 11], [9, 10], [9.5, 10.5], period=14)
        assert all(math.isnan(v) for v in atr)


class TestComputeSupertrend:
    def test_flips_direction_on_reversal(self):
        # A clean downtrend followed by a sharp rally must flip -1 -> +1.
        down = [candle(100 - i, 100 - i + 0.3, 100 - i - 0.6, 100 - i - 0.4, ts=ts_at(i)) for i in range(15)]
        up = [candle(86 + i, 86 + i + 0.8, 86 + i - 0.2, 86 + i + 0.6, ts=ts_at(15 + i)) for i in range(10)]
        candles = down + up
        line, direction = compute_supertrend(
            [c["high"] for c in candles],
            [c["low"] for c in candles],
            [c["close"] for c in candles],
            10,
            3.0,
        )
        assert direction[12] == -1  # still down mid-decline
        assert direction[-1] == 1  # flipped up after the rally
        assert not math.isnan(line[-1])

    def test_direction_zero_during_warmup(self):
        candles = flat_series(5)
        line, direction = compute_supertrend(
            [c["high"] for c in candles], [c["low"] for c in candles],
            [c["close"] for c in candles], 10, 3.0,
        )
        assert direction[0] == 0
        assert math.isnan(line[0])


class TestComputeBollinger:
    def test_mid_is_sma_and_bands_symmetric(self):
        closes = [100.0 + (i % 5) for i in range(40)]
        mid, upper, lower = compute_bollinger(closes, 20, 2.0)
        sma = compute_sma(closes, 20)
        i = 30
        assert abs(mid[i] - sma[i]) < 1e-9  # middle band == SMA
        # bands are mid +/- k*std -> symmetric about mid.
        assert abs((upper[i] - mid[i]) - (mid[i] - lower[i])) < 1e-9
        assert upper[i] > mid[i] > lower[i]
        assert all(math.isnan(v) for v in mid[:19])  # warmup

    def test_flat_series_bands_collapse_to_mid(self):
        mid, upper, lower = compute_bollinger([50.0] * 25, 20, 2.0)
        assert abs(upper[-1] - mid[-1]) < 1e-9
        assert abs(lower[-1] - mid[-1]) < 1e-9


class TestComputeMacd:
    def test_line_signal_hist_relationship(self):
        closes = [100.0 + i * 0.3 for i in range(80)]
        macd, signal, hist = compute_macd(closes, 12, 26, 9)
        # histogram == line - signal wherever both are defined.
        for m, s, h in zip(macd, signal, hist, strict=True):
            if not (math.isnan(m) or math.isnan(s)):
                assert abs(h - (m - s)) < 1e-9
            else:
                assert math.isnan(h)
        # In a steady uptrend fast EMA leads slow -> MACD line positive late.
        assert macd[-1] > 0

    def test_short_input_no_exception(self):
        macd, signal, hist = compute_macd([1.0, 2.0, 3.0], 12, 26, 9)
        assert all(math.isnan(v) for v in macd)
        assert all(math.isnan(v) for v in signal)
        assert all(math.isnan(v) for v in hist)


class TestComputeStochastic:
    def test_bounds_and_100_at_range_high(self):
        # Close equals the rolling high on the last bar -> %K raw = 100; the
        # smoothed %K stays within [0, 100].
        highs = [10 + i * 0.0 for i in range(20)]
        lows = [8.0] * 20
        closes = [9.0] * 19 + [10.0]  # last close == highest high
        k = compute_stochastic(highs, lows, closes, 14, 1)  # smooth=1 -> raw
        assert abs(k[-1] - 100.0) < 1e-9
        for v in k:
            if not math.isnan(v):
                assert 0.0 <= v <= 100.0

    def test_flat_window_is_midpoint(self):
        k = compute_stochastic([5.0] * 20, [5.0] * 20, [5.0] * 20, 14, 3)
        assert abs(k[-1] - 50.0) < 1e-9  # hh==ll -> 50.0

    def test_short_input_all_nan(self):
        assert all(math.isnan(v) for v in compute_stochastic([1, 2], [1, 2], [1, 2], 14, 3))


class TestComputeDonchian:
    def test_excludes_current_bar(self):
        # Prior 3 highs are [10,11,12]; the current bar spikes to 99 but the
        # channel must ignore it (upper stays 12) so a close can exceed it.
        highs = [10, 11, 12, 99]
        lows = [8, 9, 10, 5]
        upper, lower, mid = compute_donchian(highs, lows, 3)
        assert math.isnan(upper[2])  # index 2 still warming (needs 3 prior)
        assert upper[3] == 12  # current bar (99) excluded
        assert lower[3] == 8
        assert mid[3] == 10.0

    def test_short_input_all_nan(self):
        upper, lower, mid = compute_donchian([1, 2], [1, 2], 20)
        assert all(math.isnan(v) for v in upper)


class TestComputeRoc:
    def test_percent_change(self):
        closes = [100.0] * 12 + [110.0]  # +10% over 12 bars
        roc = compute_roc(closes, 12)
        assert all(math.isnan(v) for v in roc[:12])
        assert abs(roc[12] - 10.0) < 1e-9

    def test_negative_and_short_input(self):
        roc = compute_roc([100.0] * 12 + [90.0], 12)
        assert abs(roc[-1] + 10.0) < 1e-9
        assert all(math.isnan(v) for v in compute_roc([1.0, 2.0], 12))


class TestHeikinAshi:
    def test_ha_close_is_ohlc_average_and_open_recursion(self):
        candles = [
            candle(10, 12, 9, 11, ts=ts_at(0)),
            candle(11, 13, 10, 12, ts=ts_at(1)),
            candle(12, 14, 11, 13, ts=ts_at(2)),
        ]
        ha = heikin_ashi(candles)
        # HA close = (O+H+L+C)/4
        assert abs(ha[0]["close"] - (10 + 12 + 9 + 11) / 4) < 1e-9
        # first HA open = (open+close)/2 of the first raw candle
        assert abs(ha[0]["open"] - (10 + 11) / 2) < 1e-9
        # subsequent HA open = avg of prior HA open/close
        assert abs(ha[1]["open"] - (ha[0]["open"] + ha[0]["close"]) / 2) < 1e-9
        # HA high/low bound the HA open and close.
        for h in ha:
            assert h["high"] >= max(h["open"], h["close"])
            assert h["low"] <= min(h["open"], h["close"])

    def test_empty_no_exception(self):
        assert heikin_ashi([]) == []


class TestClassicPivots:
    def test_formulas(self):
        p = classic_pivots(110.0, 90.0, 100.0)
        pp = (110 + 90 + 100) / 3.0
        assert abs(p["pp"] - pp) < 1e-9
        assert abs(p["r1"] - (2 * pp - 90)) < 1e-9
        assert abs(p["s1"] - (2 * pp - 110)) < 1e-9
        assert abs(p["r2"] - (pp + (110 - 90))) < 1e-9
        assert abs(p["s2"] - (pp - (110 - 90))) < 1e-9
        # R1 above pivot above S1.
        assert p["r1"] > p["pp"] > p["s1"]


# ===============================================================================
# 2. REGISTRY INVARIANTS + SHARED WRAPPER CONTRACT
# ===============================================================================


class TestRegistryInvariants:
    def test_exactly_twenty_variants(self):
        assert len(VARIANTS) == 20

    def test_keys_match_documented_set(self):
        assert set(VARIANTS.keys()) == EXPECTED_KEYS

    def test_every_value_has_name_description_callable_entry(self):
        for key, spec in VARIANTS.items():
            assert set(spec.keys()) >= {"name", "description", "entry"}, key
            assert isinstance(spec["name"], str) and spec["name"], key
            assert isinstance(spec["description"], str) and spec["description"], key
            assert callable(spec["entry"]), key

    def test_default_variant_is_registered(self):
        assert DEFAULT_VARIANT in VARIANTS


@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS))
@pytest.mark.parametrize("side", [LONG, SHORT])
class TestEntryInsufficientData:
    """Every entry returns None (never raises) on too little data, both sides."""

    def test_empty_input(self, key, side):
        assert entry_of(key)([], 1000.0, 980.0, side) is None

    def test_three_flat_candles(self, key, side):
        # prev-day levels far away so level-based variants cannot pierce.
        candles = flat_series(3, price=100.0)
        assert entry_of(key)(candles, 1_000_000.0, 1.0, side) is None


@pytest.mark.parametrize("side", [LONG, SHORT])
class TestWrapperGuards:
    def test_bad_side_rejected(self, side):
        candles = flat_series(3)
        assert entry_of("inside_bar_breakout")(candles, 1000.0, 980.0, "SIDEWAYS") is None

    def test_none_candles_rejected(self, side):
        assert entry_of("inside_bar_breakout")(None, 1000.0, 980.0, side) is None


class TestRiskCapEnforced:
    """The wrapper rejects a raw setup whose risk exceeds MAX_RISK_PCT of ref."""

    def test_deep_pierce_exceeds_one_percent_is_rejected(self):
        # prev_day_level_fade: a pierce that closes back inside is a setup, but
        # a very deep pierce puts the structure stop > 1% away -> rejected.
        c0 = candle(1001, 1003, 1000.5, 1001, ts=ts_at(0))
        deep = candle(999, 1002, 980, 1001, ts=ts_at(1))  # low 980, stop ~20 pts (~2%)
        assert entry_of("prev_day_level_fade")([c0, deep], 1010.0, 1000.0, LONG) is None

    def test_shallow_pierce_within_cap_is_accepted(self):
        c0 = candle(1001, 1003, 1000.5, 1001, ts=ts_at(0))
        shallow = candle(999, 1002, 997, 1001, ts=ts_at(1))  # ~4.5 pts (~0.45%)
        setup = entry_of("prev_day_level_fade")([c0, shallow], 1010.0, 1000.0, LONG)
        assert_valid_setup(setup, LONG)
        assert setup["risk"] <= 1001.0 * MAX_RISK_PCT


# ===============================================================================
# 3. PER-FAMILY SIGNAL SCENES (trigger vs. nearby non-trigger)
# ===============================================================================


class TestInsideBarBreakout:
    def _scene_long(self):
        mother = candle(100.0, 100.5, 99.9, 100.2, ts=ts_at(0))
        inner = candle(100.1, 100.4, 100.0, 100.2, ts=ts_at(1))  # inside mother
        sig = candle(100.5, 100.7, 100.45, 100.55, ts=ts_at(2))  # closes above mother high
        return [mother, inner, sig]

    def test_long_triggers(self):
        setup = entry_of("inside_bar_breakout")(self._scene_long(), 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=100.55)
        assert setup["stop"] < 99.9  # mother low widened by buffer

    def test_long_no_trigger_when_not_beyond_mother(self):
        mother = candle(100.0, 100.5, 99.9, 100.2, ts=ts_at(0))
        inner = candle(100.1, 100.4, 100.0, 100.2, ts=ts_at(1))
        weak = candle(100.3, 100.45, 100.2, 100.4, ts=ts_at(2))  # close <= mother high
        assert entry_of("inside_bar_breakout")([mother, inner, weak], 1000.0, 980.0, LONG) is None

    def test_long_no_trigger_when_not_inside_bar(self):
        mother = candle(100.0, 100.5, 99.9, 100.2, ts=ts_at(0))
        not_inner = candle(100.1, 100.9, 100.0, 100.2, ts=ts_at(1))  # high exceeds mother
        sig = candle(100.5, 100.7, 100.45, 100.55, ts=ts_at(2))
        assert entry_of("inside_bar_breakout")([mother, not_inner, sig], 1000.0, 980.0, LONG) is None

    def test_short_triggers(self):
        mother = candle(100.2, 100.5, 99.9, 100.1, ts=ts_at(0))
        inner = candle(100.1, 100.4, 100.0, 100.2, ts=ts_at(1))
        sig = candle(99.9, 99.95, 99.7, 99.85, ts=ts_at(2))  # closes below mother low
        setup = entry_of("inside_bar_breakout")([mother, inner, sig], 1000.0, 980.0, SHORT)
        assert_valid_setup(setup, SHORT, ref_hint=99.85)
        assert setup["stop"] > 100.5  # mother high widened by buffer


class TestPrevDayLevelFade:
    def test_long_triggers(self):
        c0 = candle(1001, 1003, 1000.5, 1001, ts=ts_at(0))
        sig = candle(999, 1002, 997, 1001, ts=ts_at(1))  # pierces prev low, closes above
        setup = entry_of("prev_day_level_fade")([c0, sig], 1010.0, 1000.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=1001.0)

    def test_long_no_trigger_when_close_below_level(self):
        c0 = candle(1001, 1003, 1000.5, 1001, ts=ts_at(0))
        sig = candle(999, 999.8, 997, 999.5, ts=ts_at(1))  # closes below prev low -> not a fade
        assert entry_of("prev_day_level_fade")([c0, sig], 1010.0, 1000.0, LONG) is None

    def test_long_no_trigger_without_pierce(self):
        c0 = candle(1001, 1003, 1000.5, 1001, ts=ts_at(0))
        sig = candle(1001, 1003, 1000.5, 1002, ts=ts_at(1))  # never dips below prev low
        assert entry_of("prev_day_level_fade")([c0, sig], 1010.0, 1000.0, LONG) is None


class TestDonchianBreakout:
    def _base(self):
        # 21 tight bars inside [99, 100] establish the channel.
        return [candle(99.5, 100.0, 99.0, 99.5, 1000, ts_at(i)) for i in range(21)]

    def test_long_triggers(self):
        candles = self._base() + [candle(99.8, 100.5, 99.7, 100.4, 1000, ts_at(21))]
        setup = entry_of("donchian_breakout")(candles, 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=100.4)
        assert abs(setup["stop"] - 99.5) < 1e-9  # channel midline, no buffer

    def test_long_no_trigger_inside_channel(self):
        candles = self._base() + [candle(99.8, 99.95, 99.7, 99.9, 1000, ts_at(21))]  # below upper
        assert entry_of("donchian_breakout")(candles, 1000.0, 980.0, LONG) is None


class TestVwapBreakout:
    def _base_long(self):
        # 20 bars hugging ~100 just under VWAP.
        return [candle(100.0, 100.2, 99.8, 99.95, 1000, ts_at(i)) for i in range(20)]

    def test_long_triggers(self):
        candles = self._base_long() + [
            candle(99.9, 100.0, 99.8, 99.85, 1000, ts_at(20)),  # prev below VWAP
            candle(99.9, 100.6, 99.85, 100.5, 5000, ts_at(21)),  # crosses up on 5x volume
        ]
        setup = entry_of("vwap_breakout")(candles, 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=100.5)

    def test_long_no_trigger_without_volume_expansion(self):
        candles = self._base_long() + [
            candle(99.9, 100.0, 99.8, 99.85, 1000, ts_at(20)),
            candle(99.9, 100.6, 99.85, 100.5, 1000, ts_at(21)),  # same cross, no volume
        ]
        assert entry_of("vwap_breakout")(candles, 1000.0, 980.0, LONG) is None

    def test_short_triggers(self):
        base = [candle(100.0, 100.2, 99.8, 100.05, 1000, ts_at(i)) for i in range(20)]
        candles = base + [
            candle(100.1, 100.2, 100.0, 100.15, 1000, ts_at(20)),  # prev above VWAP
            candle(100.1, 100.15, 99.4, 99.5, 5000, ts_at(21)),  # crosses down on volume
        ]
        setup = entry_of("vwap_breakout")(candles, 1000.0, 980.0, SHORT)
        assert_valid_setup(setup, SHORT, ref_hint=99.5)


class TestOrbBreakout:
    def _base(self):
        # First 15 minutes (bars at 0/3/6/9/12/15 min) form a tight range.
        candles = [candle(100.0, 100.3, 99.8, 100.0, 1000, ts_at(i)) for i in range(6)]
        candles += [candle(100.0, 100.25, 99.85, 100.05, 1000, ts_at(i)) for i in range(6, 21)]
        return candles

    def test_long_triggers(self):
        candles = self._base() + [candle(100.1, 100.6, 100.05, 100.5, 5000, ts_at(21))]
        setup = entry_of("orb_breakout")(candles, 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=100.5)
        assert abs(setup["stop"] - (100.3 + 99.8) / 2) < 1e-9  # OR midpoint, no buffer

    def test_long_no_trigger_when_close_inside_range(self):
        candles = self._base() + [candle(100.1, 100.25, 100.05, 100.2, 5000, ts_at(21))]
        assert entry_of("orb_breakout")(candles, 1000.0, 980.0, LONG) is None


class TestSupertrendFollow:
    def _down_bars(self, n=14):
        # Tiny bearish bars (close below hl2) keep the trend down with low ATR.
        return [candle(100.02, 100.05, 99.95, 99.97, 1000, ts_at(i)) for i in range(n)]

    def test_long_triggers_on_flip(self):
        candles = self._down_bars() + [candle(100.0, 100.3, 99.95, 100.3, 1000, ts_at(14))]
        setup = entry_of("supertrend_follow")(candles, 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=100.3)

    def test_long_no_trigger_without_flip(self):
        candles = self._down_bars() + [candle(100.0, 100.1, 99.95, 100.05, 1000, ts_at(14))]
        assert entry_of("supertrend_follow")(candles, 1000.0, 980.0, LONG) is None

    def test_short_triggers_on_flip(self):
        up = [candle(99.98, 100.05, 99.95, 100.03, 1000, ts_at(i)) for i in range(14)]
        candles = up + [candle(100.0, 100.05, 99.6, 99.65, 1000, ts_at(14))]
        setup = entry_of("supertrend_follow")(candles, 1000.0, 980.0, SHORT)
        assert_valid_setup(setup, SHORT, ref_hint=99.65)


class TestBollingerReversion:
    # A gently oscillating base gives a non-zero, tight std.
    BASE_VALS = [
        100.0, 100.05, 99.95, 100.02, 99.98, 100.03, 99.97, 100.01, 99.99, 100.04,
        99.96, 100.02, 99.98, 100.0, 100.01, 99.99, 100.02, 99.98, 100.0, 100.0,
    ]

    def _base(self):
        return [candle(v, v + 0.05, v - 0.05, v, 1000, ts_at(i)) for i, v in enumerate(self.BASE_VALS)]

    def test_long_triggers(self):
        candles = self._base() + [
            candle(100.0, 100.0, 99.5, 99.6, 1000, ts_at(20)),  # prev closes below lower band
            candle(99.65, 100.0, 99.6, 99.95, 1000, ts_at(21)),  # sig closes back inside
        ]
        setup = entry_of("bollinger_reversion")(candles, 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=99.95)

    def test_long_no_trigger_when_prev_inside_band(self):
        candles = self._base() + [
            candle(100.0, 100.05, 99.95, 100.0, 1000, ts_at(20)),  # prev stays inside
            candle(99.65, 100.0, 99.6, 99.95, 1000, ts_at(21)),
        ]
        assert entry_of("bollinger_reversion")(candles, 1000.0, 980.0, LONG) is None


class TestEmaRibbonTrend:
    def _uptrend(self, n=58):
        # Gentle uptrend so EMA 8/21/55 stack for LONG.
        closes = [1000.0 + 0.25 * i for i in range(n)]
        return [candle(c - 0.05, c + 0.15, c - 0.15, c, 1000, ts_at(i)) for i, c in enumerate(closes)]

    def _long_scene(self):
        candles = self._uptrend()
        closes = [c["close"] for c in candles]
        e21 = compute_ema(closes, 21)
        pull_close = closes[-2]
        # Pullback candle (index -2) dips so its range straddles EMA21.
        candles[-2] = candle(pull_close + 0.1, pull_close + 0.2, e21[-2] - 0.1, pull_close, 1000, ts_at(len(candles) - 2))
        # Signal candle closes above the pullback high.
        candles[-1] = candle(pull_close + 0.15, pull_close + 0.6, pull_close + 0.1, pull_close + 0.35, 1000, ts_at(len(candles) - 1))
        return candles, pull_close

    def test_long_triggers(self):
        candles, pull_close = self._long_scene()
        setup = entry_of("ema_ribbon_trend")(candles, 1000.0, 980.0, LONG)
        assert_valid_setup(setup, LONG, ref_hint=pull_close + 0.35)

    def test_long_no_trigger_when_sig_not_beyond_pullback(self):
        candles, pull_close = self._long_scene()
        # Signal fails to close above the pullback high.
        candles[-1] = candle(pull_close + 0.15, pull_close + 0.19, pull_close + 0.1, pull_close + 0.15, 1000, ts_at(len(candles) - 1))
        assert entry_of("ema_ribbon_trend")(candles, 1000.0, 980.0, LONG) is None


# ===============================================================================
# 4. DETERMINISM + VARIANT RESOLUTION
# ===============================================================================


class TestDeterminism:
    @pytest.mark.parametrize(
        "key,builder,side",
        [
            ("inside_bar_breakout", "inside_long", LONG),
            ("donchian_breakout", "donchian_long", LONG),
            ("supertrend_follow", "super_long", LONG),
            ("vwap_breakout", "vwap_long", LONG),
        ],
    )
    def test_repeated_calls_are_equal(self, key, builder, side):
        candles = _BUILDERS[builder]()
        fn = entry_of(key)
        r1 = fn(candles, 1000.0, 980.0, side)
        r2 = fn(candles, 1000.0, 980.0, side)
        assert r1 == r2
        assert r1 is not None  # the scenes are genuine setups

    def test_input_not_mutated(self):
        candles = _BUILDERS["donchian_long"]()
        snapshot = [dict(c) for c in candles]
        entry_of("donchian_breakout")(candles, 1000.0, 980.0, LONG)
        assert candles == snapshot


class TestResolveVariantKey:
    def test_stem_with_timestamp_suffix(self):
        key, warning = resolve_variant_key("vwap_breakout_20240520093000")
        assert key == "vwap_breakout"
        assert warning is None

    def test_plain_stem(self):
        key, warning = resolve_variant_key("orb_breakout")
        assert key == "orb_breakout"
        assert warning is None

    def test_env_fallback(self):
        key, warning = resolve_variant_key("unknown_file", env_variant="donchian_breakout")
        assert key == "donchian_breakout"
        assert warning is None

    def test_default_fallback_warns(self):
        key, warning = resolve_variant_key("garbage", env_variant="")
        assert key == DEFAULT_VARIANT
        assert warning is not None and "FALLING BACK" in warning


# Scene builders reused by the determinism block (kept local + readable).
def _inside_long():
    return [
        candle(100.0, 100.5, 99.9, 100.2, ts=ts_at(0)),
        candle(100.1, 100.4, 100.0, 100.2, ts=ts_at(1)),
        candle(100.5, 100.7, 100.45, 100.55, ts=ts_at(2)),
    ]


def _donchian_long():
    base = [candle(99.5, 100.0, 99.0, 99.5, 1000, ts_at(i)) for i in range(21)]
    return base + [candle(99.8, 100.5, 99.7, 100.4, 1000, ts_at(21))]


def _super_long():
    down = [candle(100.02, 100.05, 99.95, 99.97, 1000, ts_at(i)) for i in range(14)]
    return down + [candle(100.0, 100.3, 99.95, 100.3, 1000, ts_at(14))]


def _vwap_long():
    base = [candle(100.0, 100.2, 99.8, 99.95, 1000, ts_at(i)) for i in range(20)]
    return base + [
        candle(99.9, 100.0, 99.8, 99.85, 1000, ts_at(20)),
        candle(99.9, 100.6, 99.85, 100.5, 5000, ts_at(21)),
    ]


_BUILDERS = {
    "inside_long": _inside_long,
    "donchian_long": _donchian_long,
    "super_long": _super_long,
    "vwap_long": _vwap_long,
}
