"""Offline unit tests for the daily capital allocation core.

Covers ``resolve_allocation`` (percent/amount modes, the never-exceed-available
clamp, and defensive coercion of junk input) and ``parse_funds`` (broker payloads
arrive as preformatted strings). This decides how much real capital is committed,
so the numbers are pinned exactly. No DB, no network.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test" * 16)
os.environ.setdefault("APP_KEY", "test" * 16)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from services.capital_service import parse_funds, resolve_allocation  # noqa: E402


class TestResolveAllocationPercent:
    def test_half_of_available(self):
        out = resolve_allocation(100000.0, "percent", 0.0, 50.0)
        assert out["allocated"] == 50000.0
        assert out["available_cash"] == 100000.0
        assert out["clamped"] is False

    def test_full_available_is_default_shape(self):
        out = resolve_allocation(250000.0, "percent", 0.0, 100.0)
        assert out["allocated"] == 250000.0
        assert out["clamped"] is False

    def test_zero_percent_allocates_nothing(self):
        assert resolve_allocation(100000.0, "percent", 0.0, 0.0)["allocated"] == 0.0

    def test_percent_over_100_clamped_to_100(self):
        # Cannot allocate 150% of the account.
        out = resolve_allocation(100000.0, "percent", 0.0, 150.0)
        assert out["percent"] == 100.0
        assert out["allocated"] == 100000.0

    def test_negative_percent_clamped_to_zero(self):
        out = resolve_allocation(100000.0, "percent", 0.0, -20.0)
        assert out["percent"] == 0.0
        assert out["allocated"] == 0.0

    def test_rounds_to_paise(self):
        out = resolve_allocation(10000.0, "percent", 0.0, 33.333)
        assert out["allocated"] == round(10000.0 * 0.33333, 2)


class TestResolveAllocationAmount:
    def test_fixed_amount_under_available(self):
        out = resolve_allocation(100000.0, "amount", 25000.0, 100.0)
        assert out["allocated"] == 25000.0
        assert out["clamped"] is False

    def test_amount_above_available_is_clamped(self):
        # The core safety rule: never allocate capital the account lacks.
        out = resolve_allocation(50000.0, "amount", 200000.0, 100.0)
        assert out["allocated"] == 50000.0
        assert out["clamped"] is True

    def test_amount_equal_to_available_not_flagged(self):
        out = resolve_allocation(50000.0, "amount", 50000.0, 100.0)
        assert out["allocated"] == 50000.0
        assert out["clamped"] is False

    def test_negative_amount_treated_as_zero(self):
        out = resolve_allocation(100000.0, "amount", -500.0, 100.0)
        assert out["amount"] == 0.0
        assert out["allocated"] == 0.0

    def test_percent_ignored_in_amount_mode(self):
        out = resolve_allocation(100000.0, "amount", 10000.0, 50.0)
        assert out["allocated"] == 10000.0


class TestResolveAllocationDefensive:
    def test_unknown_mode_falls_back_to_percent(self):
        # A corrupt config must not allocate on an unintended basis.
        out = resolve_allocation(100000.0, "bogus", 99999.0, 10.0)
        assert out["mode"] == "percent"
        assert out["allocated"] == 10000.0

    def test_none_mode_falls_back_to_percent(self):
        assert resolve_allocation(1000.0, None, 0.0, 50.0)["mode"] == "percent"

    def test_negative_available_cash_is_zero(self):
        # A debit balance allocates nothing rather than a negative figure.
        out = resolve_allocation(-5000.0, "percent", 0.0, 100.0)
        assert out["available_cash"] == 0.0
        assert out["allocated"] == 0.0

    def test_unparseable_inputs_do_not_raise(self):
        out = resolve_allocation("junk", "percent", "junk", "junk")
        assert out["available_cash"] == 0.0
        assert out["allocated"] == 0.0

    def test_string_numbers_accepted(self):
        # Broker payloads arrive as strings.
        out = resolve_allocation("100000.00", "percent", "0", "25")
        assert out["allocated"] == 25000.0

    def test_zero_cash_allocates_zero_in_amount_mode(self):
        out = resolve_allocation(0.0, "amount", 10000.0, 100.0)
        assert out["allocated"] == 0.0
        assert out["clamped"] is True


class TestParseFunds:
    def test_string_payload_coerced(self):
        out = parse_funds(
            {
                "availablecash": "12345.67",
                "collateral": "1000.00",
                "utiliseddebits": "500.50",
                "m2mrealized": "-250.25",
                "m2munrealized": "75.00",
            }
        )
        assert out["availablecash"] == 12345.67
        assert out["utiliseddebits"] == 500.50
        assert out["m2mrealized"] == -250.25

    def test_missing_keys_default_zero(self):
        out = parse_funds({"availablecash": "100"})
        assert out["availablecash"] == 100.0
        assert out["collateral"] == 0.0
        assert out["utiliseddebits"] == 0.0

    def test_none_and_junk_safe(self):
        for bad in (None, {}, {"availablecash": "abc"}, "notadict"):
            out = parse_funds(bad)
            assert out["availablecash"] == 0.0
