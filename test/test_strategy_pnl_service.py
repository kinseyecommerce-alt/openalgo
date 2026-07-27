"""Offline unit tests for the pure strategy-P&L netting core.

Covers ``aggregate_strategy_pnl`` and ``attribute_fills`` only -- no DB, no
network. The average-cost intraday netting is P&L math for a trading dashboard,
so these tests pin the exact numbers.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test")
os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.strategy_pnl_service import aggregate_strategy_pnl, attribute_fills


def _fill(strategy, symbol, action, qty, price, exchange="NSE"):
    return {
        "strategy": strategy,
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "quantity": qty,
        "price": price,
    }


class TestAggregateStrategyPnl:
    def test_day_pnl_reconciles_with_rounded_legs(self):
        # Prices chosen so realized and unrealized each land on thirds of a
        # paisa: the displayed row must satisfy realized + unrealized == day_pnl
        # exactly (no independent-rounding drift).
        fills = [
            _fill("S1", "SBIN", "BUY", 3, 100.0),
            _fill("S1", "SBIN", "SELL", 1, 100.3333),  # realized ~ 0.3333
            _fill("S1", "SBIN", "BUY", 3, 100.6667),  # scale-in, avg shifts
        ]
        out = aggregate_strategy_pnl(fills, {("SBIN", "NSE"): 101.0})
        s = out["S1"]
        assert round(s["realized"] + s["unrealized"], 2) == s["day_pnl"]

    def test_clean_round_trip(self):
        fills = [
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "SELL", 10, 105.0),
        ]
        out = aggregate_strategy_pnl(fills, {})
        s = out["S1"]
        assert s["realized"] == 50.0
        assert s["unrealized"] == 0.0
        assert s["day_pnl"] == 50.0
        assert s["trades"] == 2
        assert s["open_symbol"] is None
        assert s["open_qty"] == 0
        assert s["open_side"] is None

    def test_open_long_marked_to_ltp(self):
        fills = [_fill("S1", "SBIN", "BUY", 10, 100.0)]
        out = aggregate_strategy_pnl(fills, {("SBIN", "NSE"): 103.0})
        s = out["S1"]
        assert s["realized"] == 0.0
        assert s["unrealized"] == 30.0
        assert s["day_pnl"] == 30.0
        assert s["open_symbol"] == "SBIN"
        assert s["open_qty"] == 10
        assert s["open_side"] == "LONG"

    def test_open_short_marked_to_ltp(self):
        fills = [_fill("S1", "SBIN", "SELL", 5, 200.0)]
        out = aggregate_strategy_pnl(fills, {("SBIN", "NSE"): 195.0})
        s = out["S1"]
        assert s["realized"] == 0.0
        assert s["unrealized"] == 25.0
        assert s["day_pnl"] == 25.0
        assert s["open_symbol"] == "SBIN"
        assert s["open_qty"] == -5
        assert s["open_side"] == "SHORT"

    def test_partial_close_open_long_to_ltp(self):
        # buy 10@100, sell 6@110 -> realized 6*(110-100)=60, open long 4 @ ltp
        fills = [
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "SELL", 6, 110.0),
        ]
        out = aggregate_strategy_pnl(fills, {("SBIN", "NSE"): 108.0})
        s = out["S1"]
        assert s["realized"] == 60.0
        # open 4 @ avg_buy 100 marked to 108 -> 4*8 = 32
        assert s["unrealized"] == 32.0
        assert s["day_pnl"] == 92.0
        assert s["open_symbol"] == "SBIN"
        assert s["open_qty"] == 4
        assert s["open_side"] == "LONG"

    def test_partial_close_open_long_no_ltp_uses_avg_buy(self):
        # no LTP provided -> open leg marked to its own avg_buy -> unrealized 0
        fills = [
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "SELL", 6, 110.0),
        ]
        out = aggregate_strategy_pnl(fills, {})
        s = out["S1"]
        assert s["realized"] == 60.0
        assert s["unrealized"] == 0.0
        assert s["open_qty"] == 4

    def test_short_then_cover_realized(self):
        # sell 5@200, buy 5@190 -> realized 5*(200-190)=50, flat
        fills = [
            _fill("S1", "SBIN", "SELL", 5, 200.0),
            _fill("S1", "SBIN", "BUY", 5, 190.0),
        ]
        out = aggregate_strategy_pnl(fills, {})
        s = out["S1"]
        assert s["realized"] == 50.0
        assert s["unrealized"] == 0.0
        assert s["open_side"] is None
        assert s["open_qty"] == 0

    def test_scale_in_average_cost(self):
        # buy 10@100 and buy 10@120 -> avg_buy 110; sell 10@130
        # matched 10*(130-110)=200; open long 10 @ avg 110 marked to 125 -> 150
        fills = [
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "BUY", 10, 120.0),
            _fill("S1", "SBIN", "SELL", 10, 130.0),
        ]
        out = aggregate_strategy_pnl(fills, {("SBIN", "NSE"): 125.0})
        s = out["S1"]
        assert s["realized"] == 200.0
        assert s["unrealized"] == 150.0
        assert s["day_pnl"] == 350.0
        assert s["open_qty"] == 10
        assert s["open_side"] == "LONG"

    def test_multiple_strategies_isolated(self):
        fills = [
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "SELL", 10, 105.0),
            _fill("S2", "INFY", "BUY", 5, 200.0),
            _fill("S2", "INFY", "SELL", 5, 190.0),
        ]
        out = aggregate_strategy_pnl(fills, {})
        assert out["S1"]["realized"] == 50.0
        assert out["S2"]["realized"] == -50.0
        assert set(out.keys()) == {"S1", "S2"}

    def test_multiple_symbols_within_strategy_summed(self):
        # S1 holds two symbols; realized/unrealized sum across both.
        fills = [
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "SELL", 10, 105.0),  # realized +50
            _fill("S1", "INFY", "BUY", 5, 200.0),  # open long 5
        ]
        out = aggregate_strategy_pnl(fills, {("INFY", "NSE"): 210.0})
        s = out["S1"]
        assert s["realized"] == 50.0
        assert s["unrealized"] == 50.0  # 5 * (210 - 200)
        assert s["day_pnl"] == 100.0
        assert s["trades"] == 3
        # single open leg reported (INFY)
        assert s["open_symbol"] == "INFY"
        assert s["open_qty"] == 5
        assert s["open_side"] == "LONG"

    def test_multiple_open_legs_marks_multi_and_largest(self):
        # Two open legs in one strategy -> pick largest abs, flag multi.
        fills = [
            _fill("S1", "SBIN", "BUY", 3, 100.0),
            _fill("S1", "INFY", "BUY", 8, 200.0),
        ]
        out = aggregate_strategy_pnl(fills, {})
        s = out["S1"]
        assert s["open_symbol"] == "INFY"
        assert s["open_qty"] == 8
        assert s.get("note") == "multi"

    def test_rounding_in_output(self):
        # avg_buy = (10*100.111)/10; sell matched -> keep 2dp in output
        fills = [
            _fill("S1", "SBIN", "BUY", 3, 100.005),
            _fill("S1", "SBIN", "SELL", 3, 100.015),
        ]
        out = aggregate_strategy_pnl(fills, {})
        s = out["S1"]
        # realized = 3 * (100.015 - 100.005) = 0.03
        assert s["realized"] == 0.03
        # ensure it is rounded to 2dp (float, not a long tail)
        assert s["realized"] == round(s["realized"], 2)

    def test_empty_input_returns_empty_dict(self):
        assert aggregate_strategy_pnl([], {}) == {}
        assert aggregate_strategy_pnl(None, {}) == {}

    def test_bad_qty_price_skipped(self):
        fills = [
            _fill("S1", "SBIN", "BUY", 0, 100.0),  # zero qty
            _fill("S1", "SBIN", "SELL", 5, 0.0),  # zero price
            _fill("S1", "SBIN", "BUY", 10, 100.0),
            _fill("S1", "SBIN", "SELL", 10, 105.0),
        ]
        out = aggregate_strategy_pnl(fills, {})
        s = out["S1"]
        assert s["realized"] == 50.0
        assert s["trades"] == 2  # only the two valid fills counted


class TestAttributeFills:
    def test_orderid_mapping(self):
        orders = {"O1": "Alpha", "O2": "Beta"}
        fills = [
            {"orderid": "O1", "symbol": "SBIN", "exchange": "NSE",
             "action": "BUY", "quantity": 10, "price": 100.0},
            {"orderid": "O2", "symbol": "INFY", "exchange": "NSE",
             "action": "SELL", "quantity": 5, "price": 200.0},
        ]
        out = attribute_fills(orders, fills)
        assert out[0]["strategy"] == "Alpha"
        assert out[1]["strategy"] == "Beta"
        assert out[0]["action"] == "BUY"
        assert out[0]["quantity"] == 10

    def test_unattributed_fill_gets_empty_strategy(self):
        orders = {"O1": "Alpha"}
        fills = [
            {"orderid": "UNKNOWN", "symbol": "SBIN", "exchange": "NSE",
             "action": "BUY", "quantity": 10, "price": 100.0},
        ]
        out = attribute_fills(orders, fills)
        assert out[0]["strategy"] == ""

    def test_bad_qty_price_skipped(self):
        orders = {"O1": "Alpha"}
        fills = [
            {"orderid": "O1", "symbol": "SBIN", "exchange": "NSE",
             "action": "BUY", "quantity": 0, "price": 100.0},
            {"orderid": "O1", "symbol": "SBIN", "exchange": "NSE",
             "action": "BUY", "quantity": 10, "price": -5.0},
            {"orderid": "O1", "symbol": "SBIN", "exchange": "NSE",
             "action": "BUY", "quantity": 10, "price": 100.0},
        ]
        out = attribute_fills(orders, fills)
        assert len(out) == 1
        assert out[0]["quantity"] == 10
        assert out[0]["price"] == 100.0

    def test_action_uppercased(self):
        out = attribute_fills(
            {"O1": "Alpha"},
            [{"orderid": "O1", "symbol": "SBIN", "exchange": "NSE",
              "action": "buy", "quantity": 1, "price": 10.0}],
        )
        assert out[0]["action"] == "BUY"

    def test_end_to_end_attribute_then_aggregate(self):
        orders = {"O1": "Alpha", "O2": "Alpha"}
        fills = [
            {"orderid": "O1", "symbol": "SBIN", "exchange": "NSE",
             "action": "BUY", "quantity": 10, "price": 100.0},
            {"orderid": "O2", "symbol": "SBIN", "exchange": "NSE",
             "action": "SELL", "quantity": 10, "price": 105.0},
        ]
        attributed = attribute_fills(orders, fills)
        out = aggregate_strategy_pnl(attributed, {})
        assert out["Alpha"]["realized"] == 50.0
        assert out["Alpha"]["day_pnl"] == 50.0
