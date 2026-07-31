"""Offline unit tests for the pure strategy-performance core.

Covers ``summarize_performance`` (track-record metrics) and ``resolve_range``
(date-window resolution) only -- no DB, no network. The P&L aggregation drives
a trading dashboard, so these tests pin the exact numbers.
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test")
os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.strategy_performance_service import (  # noqa: E402
    IST,
    MAX_RANGE_DAYS,
    net_fills_chronological,
    resolve_range,
    summarize_performance,
)


def _dfill(strategy, symbol, action, qty, price, day, product="MIS", exchange="NSE"):
    return {
        "strategy": strategy,
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "action": action,
        "quantity": qty,
        "price": price,
        "day": day,
    }


def _day(d, pnl, trades=1):
    return {"date": d, "pnl": pnl, "trades": trades}


class TestSummarizePerformance:
    def test_mixed_win_loss_series(self):
        daily = [
            _day("2026-01-01", 100.0, 2),
            _day("2026-01-02", -40.0, 1),
            _day("2026-01-03", 60.0, 3),
        ]
        # Closed round-trips: three wins, two losses.
        trades = [100.0, 50.0, 60.0, -40.0, -10.0]
        s = summarize_performance(daily, trades)

        assert s["total_pnl"] == 120.0
        assert s["cumulative"] == 120.0
        assert s["trading_days"] == 3
        assert s["trade_count"] == 5
        assert s["win_trades"] == 3
        assert s["loss_trades"] == 2
        # 3 wins / 5 decided = 60%.
        assert s["win_rate"] == 60.0
        # mean of [100, 50, 60] = 70.0; mean of [-40, -10] = -25.0.
        assert s["avg_win"] == 70.0
        assert s["avg_loss"] == -25.0
        # sum wins 210 / abs(sum losses) 50 = 4.2.
        assert s["profit_factor"] == 4.2
        assert s["win_days"] == 2
        assert s["loss_days"] == 1
        assert s["best_day"] == {"date": "2026-01-01", "pnl": 100.0}
        assert s["worst_day"] == {"date": "2026-01-02", "pnl": -40.0}
        # Cumulative: 100 (peak) -> 60 -> 120; deepest dip below peak is -40.
        assert s["max_drawdown"] == -40.0

    def test_cumulative_curve_echoes_pnl(self):
        daily = [
            _day("2026-02-01", 10.0),
            _day("2026-02-02", 20.0),
            _day("2026-02-03", -5.0),
        ]
        s = summarize_performance(daily, [10.0, 20.0, -5.0])
        assert s["daily"] == [
            {"date": "2026-02-01", "pnl": 10.0, "cumulative": 10.0, "trades": 1},
            {"date": "2026-02-02", "pnl": 20.0, "cumulative": 30.0, "trades": 1},
            {"date": "2026-02-03", "pnl": -5.0, "cumulative": 25.0, "trades": 1},
        ]

    def test_max_drawdown_up_down_recover(self):
        # Cumulative: 100 -> 150 (peak) -> 90 -> 60 (trough, dd=-90) -> 130.
        daily = [
            _day("2026-03-01", 100.0),
            _day("2026-03-02", 50.0),
            _day("2026-03-03", -60.0),
            _day("2026-03-04", -30.0),
            _day("2026-03-05", 70.0),
        ]
        s = summarize_performance(daily, [])
        assert s["total_pnl"] == 130.0
        # Most-negative dip below the running peak of 150 is at cum=60 -> -90.
        assert s["max_drawdown"] == -90.0
        assert s["best_day"]["pnl"] == 100.0
        assert s["worst_day"]["pnl"] == -60.0

    def test_all_wins_profit_factor_none(self):
        daily = [_day("2026-04-01", 30.0), _day("2026-04-02", 20.0)]
        s = summarize_performance(daily, [30.0, 20.0])
        assert s["win_trades"] == 2
        assert s["loss_trades"] == 0
        assert s["win_rate"] == 100.0
        assert s["profit_factor"] is None
        assert s["avg_loss"] == 0.0
        assert s["avg_win"] == 25.0

    def test_all_losses(self):
        daily = [_day("2026-05-01", -30.0), _day("2026-05-02", -20.0)]
        s = summarize_performance(daily, [-30.0, -20.0])
        assert s["win_trades"] == 0
        assert s["loss_trades"] == 2
        assert s["win_rate"] == 0.0
        # sum wins 0 / abs(sum losses) 50 = 0.0 (losses present -> not None).
        assert s["profit_factor"] == 0.0
        assert s["avg_win"] == 0.0
        assert s["avg_loss"] == -25.0
        assert s["max_drawdown"] == -50.0

    def test_breakeven_excluded_from_winrate_denominator(self):
        # Two break-even trades must not count toward wins, losses, or the
        # win-rate denominator (but still count toward trade_count).
        daily = [_day("2026-06-01", 10.0)]
        trades = [10.0, 0.0, 0.0, -5.0]
        s = summarize_performance(daily, trades)
        assert s["trade_count"] == 4
        assert s["win_trades"] == 1
        assert s["loss_trades"] == 1
        # 1 win / 2 decided = 50% (the two break-evens excluded).
        assert s["win_rate"] == 50.0

    def test_empty_is_zeroed(self):
        s = summarize_performance([], [])
        assert s["total_pnl"] == 0.0
        assert s["trading_days"] == 0
        assert s["trade_count"] == 0
        assert s["win_rate"] == 0.0
        assert s["profit_factor"] is None
        assert s["max_drawdown"] == 0.0
        assert s["best_day"] is None
        assert s["worst_day"] is None
        assert s["daily"] == []

    def test_single_day(self):
        s = summarize_performance([_day("2026-07-01", 42.5, 4)], [42.5])
        assert s["total_pnl"] == 42.5
        assert s["trading_days"] == 1
        assert s["best_day"] == {"date": "2026-07-01", "pnl": 42.5}
        assert s["worst_day"] == {"date": "2026-07-01", "pnl": 42.5}
        assert s["max_drawdown"] == 0.0
        assert s["daily"] == [
            {"date": "2026-07-01", "pnl": 42.5, "cumulative": 42.5, "trades": 4}
        ]

    def test_deterministic(self):
        daily = [_day("2026-01-01", 5.0), _day("2026-01-02", -3.0)]
        trades = [5.0, -3.0]
        assert summarize_performance(daily, trades) == summarize_performance(daily, trades)


class TestResolveRange:
    def test_default_30_days(self):
        start, end = resolve_range()
        today = datetime.now(IST).date()
        assert end == today
        assert start == today - timedelta(days=29)
        assert (end - start).days == 29

    def test_days_param(self):
        start, end = resolve_range(days=7)
        today = datetime.now(IST).date()
        assert end == today
        assert start == today - timedelta(days=6)

    def test_explicit_start_end(self):
        start, end = resolve_range(start="2026-01-01", end="2026-01-15")
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 15)

    def test_explicit_accepts_date_objects(self):
        start, end = resolve_range(start=date(2026, 2, 1), end=date(2026, 2, 10))
        assert start == date(2026, 2, 1)
        assert end == date(2026, 2, 10)

    def test_start_after_end_rejected(self):
        with pytest.raises(ValueError):
            resolve_range(start="2026-02-01", end="2026-01-01")

    def test_span_clamped_to_max(self):
        # A 400-day explicit window is pulled forward to MAX_RANGE_DAYS.
        start, end = resolve_range(start="2025-01-01", end="2026-02-05")
        assert (end - start).days == MAX_RANGE_DAYS - 1
        assert end == date(2026, 2, 5)

    def test_days_clamped_to_max(self):
        start, end = resolve_range(days=500)
        assert (end - start).days == MAX_RANGE_DAYS - 1


class TestNetFillsChronological:
    def test_same_day_round_trip(self):
        fills = [
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01"),
            _dfill("S1", "SBIN", "SELL", 10, 105.0, "2026-01-01"),
        ]
        daily, trades, untagged = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-01"]["pnl"] == 50.0
        assert daily["S1"]["2026-01-01"]["trades"] == 2
        assert trades["S1"] == [50.0]
        assert untagged == {}

    def test_cross_day_carry_counts_the_trade(self):
        # Opened day 1 (only a buy), closed day 2 (only a sell). Per-day netting
        # would report matched==0 both days and omit the trade; chronological
        # netting must attribute the whole P&L to the closing day.
        fills = [
            _dfill("S1", "ACC", "BUY", 10, 100.0, "2026-01-01", product="CNC"),
            _dfill("S1", "ACC", "SELL", 10, 130.0, "2026-01-03", product="CNC"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        # Day 1 has the opening fill but no realized P&L yet.
        assert daily["S1"]["2026-01-01"]["pnl"] == 0.0
        assert daily["S1"]["2026-01-01"]["trades"] == 1
        # Day 3 realizes the full 10 * (130 - 100) = 300.
        assert daily["S1"]["2026-01-03"]["pnl"] == 300.0
        assert daily["S1"]["2026-01-03"]["trades"] == 1
        assert trades["S1"] == [300.0]

    def test_different_products_not_netted(self):
        # A CNC long and an MIS short on the same symbol are two independent
        # OPEN positions, not a round-trip: no realized P&L, no closed trade.
        fills = [
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01", product="CNC"),
            _dfill("S1", "SBIN", "SELL", 10, 105.0, "2026-01-01", product="MIS"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-01"]["pnl"] == 0.0
        assert daily["S1"]["2026-01-01"]["trades"] == 2
        assert "S1" not in trades  # nothing closed

    def test_scale_in_then_full_exit_average_cost(self):
        # buy 10@100, buy 10@120 (avg 110), sell 20@130 -> 20*(130-110)=400.
        fills = [
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01"),
            _dfill("S1", "SBIN", "BUY", 10, 120.0, "2026-01-02"),
            _dfill("S1", "SBIN", "SELL", 20, 130.0, "2026-01-03"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-03"]["pnl"] == 400.0
        assert trades["S1"] == [400.0]

    def test_partial_exit_then_close_two_realizations_one_round_trip(self):
        # buy 10@100; sell 6@110 (realized 60, still long 4); sell 4@120
        # (realized 4*20=80). One round-trip of 140, realized split across days.
        fills = [
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01"),
            _dfill("S1", "SBIN", "SELL", 6, 110.0, "2026-01-02"),
            _dfill("S1", "SBIN", "SELL", 4, 120.0, "2026-01-03"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-02"]["pnl"] == 60.0
        assert daily["S1"]["2026-01-03"]["pnl"] == 80.0
        # A single lifecycle -> one round-trip result equal to the accumulated
        # realized (60 + 80 = 140).
        assert trades["S1"] == [140.0]

    def test_flip_through_zero_closes_and_reopens(self):
        # long 10@100; sell 15@110 -> close 10 (realized 100), flip to short 5
        # @110; buy 5@105 -> cover short (realized 5*(110-105)=25).
        fills = [
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01"),
            _dfill("S1", "SBIN", "SELL", 15, 110.0, "2026-01-02"),
            _dfill("S1", "SBIN", "BUY", 5, 105.0, "2026-01-03"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-02"]["pnl"] == 100.0
        assert daily["S1"]["2026-01-03"]["pnl"] == 25.0
        # Two distinct round-trips: the long (100) then the short (25).
        assert trades["S1"] == [100.0, 25.0]

    def test_open_position_left_unrealized_not_counted(self):
        fills = [_dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01")]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-01"]["pnl"] == 0.0
        assert "S1" not in trades

    def test_untagged_bucket_separate(self):
        fills = [
            _dfill("", "SBIN", "BUY", 10, 100.0, "2026-01-01"),
            _dfill("", "SBIN", "SELL", 10, 108.0, "2026-01-01"),
        ]
        daily, trades, untagged = net_fills_chronological(fills)
        # Untagged realized surfaces in untagged_daily, never as a strategy trade.
        assert untagged["2026-01-01"] == 80.0
        assert trades == {}

    def test_short_round_trip(self):
        # sell 5@200 (open short), buy 5@190 -> realized 5*(200-190)=50.
        fills = [
            _dfill("S1", "SBIN", "SELL", 5, 200.0, "2026-01-01"),
            _dfill("S1", "SBIN", "BUY", 5, 190.0, "2026-01-02"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-02"]["pnl"] == 50.0
        assert trades["S1"] == [50.0]

    def test_out_of_order_days_sorted(self):
        # Provide the close before the open; day-sort must still net correctly.
        fills = [
            _dfill("S1", "SBIN", "SELL", 10, 130.0, "2026-01-03", product="CNC"),
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01", product="CNC"),
        ]
        daily, trades, _ = net_fills_chronological(fills)
        assert daily["S1"]["2026-01-03"]["pnl"] == 300.0
        assert trades["S1"] == [300.0]

    def test_two_strategies_isolated(self):
        fills = [
            _dfill("S1", "SBIN", "BUY", 10, 100.0, "2026-01-01"),
            _dfill("S1", "SBIN", "SELL", 10, 105.0, "2026-01-01"),
            _dfill("S2", "INFY", "BUY", 5, 200.0, "2026-01-01"),
            _dfill("S2", "INFY", "SELL", 5, 190.0, "2026-01-01"),
        ]
        _daily, trades, _ = net_fills_chronological(fills)
        assert trades["S1"] == [50.0]
        assert trades["S2"] == [-50.0]

    def test_empty(self):
        daily, trades, untagged = net_fills_chronological([])
        assert daily == {}
        assert trades == {}
        assert untagged == {}


class TestLiveFillsDayGuard:
    """The live builder must refuse any non-today day (broker tradebook is not
    historical), so it can never fabricate a past-day track record by joining a
    stale OrderLog against today's tradebook. The guard returns [] BEFORE any DB
    or network access, so this runs fully offline."""

    def test_past_day_returns_empty_without_db(self):
        from services.strategy_pnl_service import build_attributed_fills_live

        yesterday = datetime.now(IST).date() - timedelta(days=1)
        # No DB is configured for real broker/order data here; the guard must
        # short-circuit to [] before touching the database at all.
        assert build_attributed_fills_live("user1", yesterday) == []

    def test_far_past_day_returns_empty(self):
        from services.strategy_pnl_service import build_attributed_fills_live

        assert build_attributed_fills_live("user1", date(2020, 1, 1)) == []
