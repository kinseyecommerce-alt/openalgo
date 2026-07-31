"""Offline unit tests for the pre-trade capital / position-count guard.

The single most important property under test: an exposure-REDUCING order (an
exit) is never blocked, even when every limit is breached. A cap that trapped a
losing position would be worse than no cap at all. No DB, no network.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test" * 16)
os.environ.setdefault("APP_KEY", "test" * 16)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.capital_guard_service import (  # noqa: E402
    check_order,
    is_increasing,
    summarize_positions,
)


def _pos(symbol, qty, avg, exchange="NSE", product="MIS"):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "quantity": qty,
        "average_price": avg,
    }


def _order(symbol, action, qty, exchange="NSE", product="MIS"):
    return {
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "action": action,
        "quantity": qty,
    }


class TestSummarizePositions:
    def test_deployed_and_count(self):
        out = summarize_positions([_pos("SBIN", 100, 800.0), _pos("INFY", -50, 1500.0)])
        # 100*800 + 50*1500 = 80000 + 75000
        assert out["deployed"] == 155000.0
        assert out["open_count"] == 2

    def test_flat_legs_ignored(self):
        # Squared-off rows stay in the broker's book but hold no capital.
        out = summarize_positions([_pos("SBIN", 0, 800.0), _pos("INFY", 10, 1500.0)])
        assert out["open_count"] == 1
        assert out["deployed"] == 15000.0

    def test_duplicate_legs_summed_not_overwritten(self):
        out = summarize_positions([_pos("SBIN", 10, 100.0), _pos("SBIN", 5, 100.0)])
        assert out["by_key"][("SBIN", "NSE", "MIS")] == 15

    def test_junk_rows_skipped(self):
        out = summarize_positions([{"symbol": "X", "quantity": "abc"}, _pos("SBIN", 10, 100.0)])
        assert out["open_count"] == 1

    def test_empty(self):
        out = summarize_positions([])
        assert out == {"deployed": 0.0, "open_count": 0, "by_key": {}}


class TestIsIncreasing:
    def test_buy_from_flat_or_long_increases(self):
        assert is_increasing("BUY", 0) is True
        assert is_increasing("BUY", 100) is True

    def test_buy_against_short_reduces(self):
        assert is_increasing("BUY", -100) is False

    def test_sell_from_flat_or_short_increases(self):
        assert is_increasing("SELL", 0) is True
        assert is_increasing("SELL", -50) is True

    def test_sell_against_long_reduces(self):
        assert is_increasing("SELL", 100) is False

    def test_unknown_action_treated_as_increasing(self):
        # Must not let an unrecognised action masquerade as an exit.
        assert is_increasing("WHATEVER", 100) is True


class TestExitsAreNeverBlocked:
    """The guard's most important guarantee."""

    def test_exit_allowed_when_capital_exhausted(self):
        positions = [_pos("SBIN", 100, 10000.0)]  # 10,00,000 deployed
        allowed, reason, detail = check_order(
            _order("SBIN", "SELL", 100), positions, 1000000.0, 10, 10000.0
        )
        assert allowed is True
        assert reason is None
        assert detail["is_exit"] is True

    def test_exit_allowed_when_position_count_full(self):
        positions = [_pos(f"S{i}", 10, 100.0) for i in range(10)]
        allowed, _reason, detail = check_order(
            _order("S0", "SELL", 10), positions, 1000.0, 10, 100.0
        )
        assert allowed is True
        assert detail["is_exit"] is True

    def test_short_cover_allowed_when_over_limits(self):
        positions = [_pos("SBIN", -100, 10000.0)]
        allowed, _reason, _d = check_order(
            _order("SBIN", "BUY", 100), positions, 0.0, 1, 10000.0
        )
        assert allowed is True

    def test_partial_exit_allowed(self):
        positions = [_pos("SBIN", 100, 10000.0)]
        allowed, _r, _d = check_order(
            _order("SBIN", "SELL", 40), positions, 0.0, 1, 10000.0
        )
        assert allowed is True


class TestCapitalLimit:
    def test_entry_within_allocation_allowed(self):
        # 10 lakh allocated, nothing deployed, order needs 80,000.
        allowed, reason, detail = check_order(
            _order("SBIN", "BUY", 100), [], 1000000.0, 10, 800.0
        )
        assert allowed is True
        assert reason is None
        assert detail["order_value"] == 80000.0

    def test_entry_exceeding_allocation_blocked(self):
        # 9,50,000 already deployed against a 10,00,000 allocation; this order
        # needs 80,000 -> would total 10,30,000.
        positions = [_pos("INFY", 950, 1000.0)]
        allowed, reason, _d = check_order(
            _order("SBIN", "BUY", 100), positions, 1000000.0, 10, 800.0
        )
        assert allowed is False
        assert "capital allocation exceeded" in reason.lower()
        assert "exits are still allowed" in reason.lower()

    def test_exactly_at_allocation_allowed(self):
        # Boundary: deployed + order == allocated is within the limit.
        positions = [_pos("INFY", 920, 1000.0)]  # 9,20,000
        allowed, _r, _d = check_order(
            _order("SBIN", "BUY", 100), positions, 1000000.0, 10, 800.0
        )
        assert allowed is True

    def test_one_rupee_over_blocked(self):
        positions = [_pos("INFY", 920, 1000.0)]  # 9,20,000
        allowed, _r, _d = check_order(
            _order("SBIN", "BUY", 100), positions, 999999.0, 10, 800.0
        )
        assert allowed is False

    def test_zero_allocation_blocks_new_entry(self):
        allowed, _r, _d = check_order(_order("SBIN", "BUY", 10), [], 0.0, 10, 800.0)
        assert allowed is False

    def test_unpriceable_order_skips_capital_check(self):
        # No price -> the cap cannot be applied; reported, not silently enforced.
        allowed, _r, detail = check_order(_order("SBIN", "BUY", 10), [], 1000.0, 10, 0.0)
        assert allowed is True
        assert detail["capital_check_skipped"] is True


class TestPositionCountLimit:
    def test_new_position_blocked_at_limit(self):
        positions = [_pos(f"S{i}", 10, 100.0) for i in range(10)]
        allowed, reason, _d = check_order(
            _order("NEWSYM", "BUY", 1), positions, 10000000.0, 10, 100.0
        )
        assert allowed is False
        assert "max concurrent positions" in reason.lower()

    def test_new_position_allowed_below_limit(self):
        positions = [_pos(f"S{i}", 10, 100.0) for i in range(9)]
        allowed, _r, _d = check_order(
            _order("NEWSYM", "BUY", 1), positions, 10000000.0, 10, 100.0
        )
        assert allowed is True

    def test_scaling_into_existing_position_not_counted(self):
        # Adding to a held symbol consumes no new slot, even at the cap.
        positions = [_pos(f"S{i}", 10, 100.0) for i in range(10)]
        allowed, _r, _d = check_order(
            _order("S3", "BUY", 5), positions, 10000000.0, 10, 100.0
        )
        assert allowed is True

    def test_flat_leg_does_not_consume_a_slot(self):
        # 10 rows but one is squared off -> only 9 open, so a new entry fits.
        positions = [_pos(f"S{i}", 10, 100.0) for i in range(9)] + [_pos("OLD", 0, 100.0)]
        allowed, _r, _d = check_order(
            _order("NEWSYM", "BUY", 1), positions, 10000000.0, 10, 100.0
        )
        assert allowed is True

    def test_non_positive_limit_disables_count_check(self):
        positions = [_pos(f"S{i}", 10, 100.0) for i in range(50)]
        allowed, _r, _d = check_order(
            _order("NEWSYM", "BUY", 1), positions, 10000000.0, 0, 100.0
        )
        assert allowed is True


class TestGuardDisabled:
    def test_disabled_allows_everything(self):
        positions = [_pos(f"S{i}", 100, 10000.0) for i in range(20)]
        allowed, reason, _d = check_order(
            _order("NEWSYM", "BUY", 1000), positions, 0.0, 1, 10000.0, enabled=False
        )
        assert allowed is True
        assert reason is None


class TestProductIsolation:
    def test_same_symbol_different_product_is_a_new_position(self):
        # A CNC holding and an MIS trade in the same symbol are separate legs,
        # so the MIS entry is a NEW position for the count.
        positions = [_pos("SBIN", 10, 100.0, product="CNC")]
        allowed, _r, detail = check_order(
            _order("SBIN", "BUY", 5, product="MIS"), positions, 10000000.0, 1, 100.0
        )
        # Count limit of 1 is already used by the CNC leg -> blocked.
        assert allowed is False
        assert detail["open_count"] == 1

    def test_sell_mis_while_holding_cnc_long_is_an_entry_not_an_exit(self):
        # Selling MIS does not close a CNC long; it opens a short leg.
        positions = [_pos("SBIN", 10, 100.0, product="CNC")]
        _allowed, _r, detail = check_order(
            _order("SBIN", "SELL", 5, product="MIS"), positions, 10000000.0, 10, 100.0
        )
        assert detail["is_exit"] is False
