"""Offline unit tests for the MCX resolver's pure core.

Covers parse_expiry across the formats OpenAlgo/Kite emit (+ junk -> None),
pick_front_month (nearest future, min_days early-roll, all-past/empty/unparseable
edge cases, today-on-expiry boundary), build_mcx_symbol (documented master
contract format, space-strip + FUT-append rule), and normalize_base_names
(comma/newline/comments/dedupe/case + "NATURAL GAS" -> "NATURALGAS"). No network,
no SDK.
"""

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from mcx_resolver import (
    _search_symbol,
    build_mcx_symbol,
    normalize_base_names,
    parse_expiry,
    pick_front_month,
)


class _FakeClient:
    """Minimal stand-in for the openalgo SDK client: canned search() response."""

    def __init__(self, rows):
        self._rows = rows

    def search(self, query=None, exchange=None):
        # Substring match, mimicking Kite's ilike("%term%") behavior, so a query
        # for CRUDEOIL also surfaces CRUDEOILM (the collision the resolver guards).
        q = (query or "").upper()
        data = [r for r in self._rows if q in str(r.get("name", "")).upper()]
        return {"status": "success", "data": data}


class TestParseExpiry:
    def test_dd_mon_yy(self):
        assert parse_expiry("26-AUG-25") == (2025, 8, 26)

    def test_dd_mon_yyyy(self):
        assert parse_expiry("26-AUG-2025") == (2025, 8, 26)

    def test_iso(self):
        assert parse_expiry("2025-08-26") == (2025, 8, 26)

    def test_compact(self):
        assert parse_expiry("28AUG25") == (2025, 8, 28)

    def test_numeric_month_rejected(self):
        # A numeric-month date is ambiguous (day-vs-month) and Kite never emits
        # it; the resolver must reject it, not silently mis-parse to Feb 1.
        assert parse_expiry("01-02-2025") is None


class TestSearchSymbolAnchoring:
    # Both the full CRUDEOIL and the mini CRUDEOILM share the same expiry, and
    # Kite's substring search returns both for query "CRUDEOIL".
    ROWS = [
        {
            "name": "CRUDEOILM",
            "symbol": "CRUDEOILM18AUG25FUT",
            "expiry": "18-AUG-25",
            "instrumenttype": "FUT",
        },
        {
            "name": "CRUDEOIL",
            "symbol": "CRUDEOIL18AUG25FUT",
            "expiry": "18-AUG-25",
            "instrumenttype": "FUT",
        },
    ]

    def test_exact_name_wins_over_mini(self):
        # base CRUDEOIL must resolve to the full contract, never the mini.
        client = _FakeClient(self.ROWS)
        assert _search_symbol(client, "CRUDEOIL", "18-AUG-25") == "CRUDEOIL18AUG25FUT"

    def test_mini_resolves_to_itself(self):
        client = _FakeClient(self.ROWS)
        assert _search_symbol(client, "CRUDEOILM", "18-AUG-25") == "CRUDEOILM18AUG25FUT"

    def test_no_expiry_match_returns_none(self):
        client = _FakeClient(self.ROWS)
        assert _search_symbol(client, "CRUDEOIL", "17-SEP-25") is None

    def test_prefix_without_exact_name_needs_digit_anchor(self):
        # Only a mini exists (name GOLDM); a query for GOLD must NOT accept it
        # via prefix, because the char after "GOLD" is "M", not an expiry digit.
        rows = [
            {
                "name": "GOLDM",
                "symbol": "GOLDM05AUG25FUT",
                "expiry": "05-AUG-25",
                "instrumenttype": "FUT",
            }
        ]
        assert _search_symbol(_FakeClient(rows), "GOLD", "05-AUG-25") is None

    def test_skips_non_fut(self):
        rows = [
            {
                "name": "CRUDEOIL",
                "symbol": "CRUDEOIL18AUG25CE",
                "expiry": "18-AUG-25",
                "instrumenttype": "CE",
            }
        ]
        assert _search_symbol(_FakeClient(rows), "CRUDEOIL", "18-AUG-25") is None

    def test_compact_full_year(self):
        assert parse_expiry("28AUG2025") == (2025, 8, 28)

    def test_lowercase_month(self):
        assert parse_expiry("26-aug-25") == (2025, 8, 26)

    def test_whitespace_tolerated(self):
        assert parse_expiry("  26-AUG-25  ") == (2025, 8, 26)

    @pytest.mark.parametrize("junk", ["", "   ", "NOTADATE", "2025/13/40", "99-XYZ-99", "32-JAN-25"])
    def test_junk_returns_none(self, junk):
        assert parse_expiry(junk) is None

    @pytest.mark.parametrize("bad", [None, 12345, ["26-AUG-25"], {"expiry": "26-AUG-25"}])
    def test_non_string_returns_none(self, bad):
        assert parse_expiry(bad) is None


class TestPickFrontMonth:
    # A realistic MCX FUT ladder, deliberately out of order.
    LADDER = ["26-NOV-25", "18-AUG-25", "17-SEP-25", "20-OCT-25"]

    def test_nearest_future_chosen(self):
        # today well before all expiries -> the earliest (AUG) wins.
        assert pick_front_month(self.LADDER, (2025, 8, 1), min_days=0) == "18-AUG-25"

    def test_min_days_rolls_to_next(self):
        # today = 15-AUG-25; AUG expiry is 3 days away. min_days=5 skips it,
        # rolling to the SEP contract.
        assert pick_front_month(self.LADDER, (2025, 8, 15), min_days=0) == "18-AUG-25"
        assert pick_front_month(self.LADDER, (2025, 8, 15), min_days=5) == "17-SEP-25"

    def test_today_on_expiry_min_days_zero_qualifies(self):
        # Expiry == today qualifies when min_days=0 (>= boundary is inclusive).
        assert pick_front_month(self.LADDER, (2025, 8, 18), min_days=0) == "18-AUG-25"

    def test_today_on_expiry_min_days_positive_skips(self):
        # Same day but min_days=1 pushes the threshold past it -> next contract.
        assert pick_front_month(self.LADDER, (2025, 8, 18), min_days=1) == "17-SEP-25"

    def test_all_past_returns_none(self):
        assert pick_front_month(self.LADDER, (2026, 1, 1), min_days=0) is None

    def test_unparseable_dropped(self):
        expiries = ["GARBAGE", "not-a-date", "17-SEP-25", "18-AUG-25"]
        assert pick_front_month(expiries, (2025, 8, 1), min_days=0) == "18-AUG-25"

    def test_all_unparseable_returns_none(self):
        assert pick_front_month(["x", "y", "z"], (2025, 8, 1), min_days=0) is None

    def test_empty_returns_none(self):
        assert pick_front_month([], (2025, 8, 1), min_days=0) is None

    def test_returns_raw_string_form(self):
        # Mixed formats: the chosen value is the RAW input string, unmodified.
        expiries = ["2025-09-17", "18AUG25"]
        assert pick_front_month(expiries, (2025, 8, 1), min_days=0) == "18AUG25"


class TestBuildMcxSymbol:
    def test_documented_example(self):
        # CLAUDE.md example: CRUDEOILM + 20MAY24 + FUT (day 20, May, 2024).
        assert build_mcx_symbol("CRUDEOILM", "20-MAY-24", 20, 5, 2024) == "CRUDEOILM20MAY24FUT"

    def test_full_year_and_two_digit_year_equivalent(self):
        # Master contract uses %y (2-digit); full or short year yield the same.
        assert build_mcx_symbol("GOLDM", "05-AUG-25", 5, 8, 2025) == "GOLDM05AUG25FUT"
        assert build_mcx_symbol("GOLDM", "05-AUG-25", 5, 8, 25) == "GOLDM05AUG25FUT"

    def test_zero_padded_day(self):
        assert build_mcx_symbol("SILVERM", "01-SEP-25", 1, 9, 2025) == "SILVERM01SEP25FUT"

    def test_space_strip_and_upper_rule(self):
        # Matches master contract: spaces removed, appended 'FUT', upper-cased.
        assert build_mcx_symbol("natural gas", "26-NOV-25", 26, 11, 2025) == "NATURALGAS26NOV25FUT"

    def test_expiry_str_not_required_for_construction(self):
        # Components are authoritative; expiry_str is ignored for the build.
        assert build_mcx_symbol("COPPER", None, 30, 6, 2025) == "COPPER30JUN25FUT"

    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            build_mcx_symbol("CRUDEOIL", "x", 1, 13, 2025)


class TestNormalizeBaseNames:
    def test_comma_separated(self):
        assert normalize_base_names("CRUDEOIL,GOLDM,SILVERM") == ["CRUDEOIL", "GOLDM", "SILVERM"]

    def test_newline_separated(self):
        assert normalize_base_names("CRUDEOIL\nGOLDM\nCOPPER") == ["CRUDEOIL", "GOLDM", "COPPER"]

    def test_lowercase_upcased(self):
        assert normalize_base_names("crudeoil, goldm") == ["CRUDEOIL", "GOLDM"]

    def test_dedupe_preserves_order(self):
        assert normalize_base_names("GOLDM,CRUDEOIL,GOLDM,SILVERM") == [
            "GOLDM",
            "CRUDEOIL",
            "SILVERM",
        ]

    def test_comments_and_blanks_dropped(self):
        text = "# my MCX list\nCRUDEOIL\n\n  \n# comment\nGOLDM\n"
        assert normalize_base_names(text) == ["CRUDEOIL", "GOLDM"]

    def test_natural_gas_alias(self):
        assert normalize_base_names("NATURAL GAS") == ["NATURALGAS"]

    def test_alias_mixed_with_plain(self):
        assert normalize_base_names("natural gas, crude oil, COPPER") == [
            "NATURALGAS",
            "CRUDEOIL",
            "COPPER",
        ]

    def test_none_returns_empty(self):
        assert normalize_base_names(None) == []

    def test_empty_returns_empty(self):
        assert normalize_base_names("") == []
        assert normalize_base_names("   \n # only a comment\n") == []
