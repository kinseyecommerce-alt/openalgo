"""Offline unit tests for the NFO index-options resolver's pure core.

Covers the ATM offset ring, option-type parsing, DDMMMYY conversion, weekly vs
monthly expiry selection, the FUT/option split that guards the shared NFO.txt,
symbol merging, and an end-to-end resolve against a canned SDK client. No
network, no SDK.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from nfo_options_resolver import (  # noqa: E402
    build_offsets,
    is_monthly_expiry,
    merge_symbols,
    normalize_option_types,
    pick_expiry,
    resolve_index_options,
    split_fut_and_opt,
    to_ddmmmyy,
)


class _FakeClient:
    """Stand-in for the openalgo SDK client.

    ``expiry`` returns canned expiries; ``optionsymbol`` synthesises a symbol
    from the offset so the ring can be asserted end to end.
    """

    def __init__(self, expiries, fail_offsets=(), expiry_raises=False):
        self._expiries = list(expiries)
        self._fail = set(fail_offsets)
        self._expiry_raises = expiry_raises
        self.calls = []

    def expiry(self, symbol=None, exchange=None, instrumenttype=None):
        if self._expiry_raises:
            raise RuntimeError("boom")
        return {"status": "success", "data": list(self._expiries)}

    def optionsymbol(
        self, underlying=None, exchange=None, expiry_date=None, offset=None, option_type=None
    ):
        self.calls.append((underlying, expiry_date, offset, option_type))
        if offset in self._fail:
            return {"status": "error", "message": "no such strike"}
        return {
            "status": "success",
            "symbol": f"{underlying}{expiry_date}{offset}{option_type}",
            "lotsize": 75,
        }


class TestBuildOffsets:
    def test_default_ring_is_money_ordered(self):
        assert build_offsets(2) == ["ITM2", "ITM1", "ATM", "OTM1", "OTM2"]

    def test_zero_is_atm_only(self):
        assert build_offsets(0) == ["ATM"]

    def test_negative_treated_as_zero(self):
        assert build_offsets(-3) == ["ATM"]

    def test_capped_at_sdk_limit(self):
        # SDK supports ITM1-50 / OTM1-50; a larger request must not emit ITM51.
        out = build_offsets(99)
        assert len(out) == 101
        assert "ITM50" in out and "OTM50" in out
        assert not any(o in out for o in ("ITM51", "OTM51"))


class TestNormalizeOptionTypes:
    def test_both(self):
        assert normalize_option_types("CE,PE") == ["CE", "PE"]

    def test_case_and_spaces(self):
        assert normalize_option_types(" ce , pe ") == ["CE", "PE"]

    def test_single(self):
        assert normalize_option_types("PE") == ["PE"]

    def test_invalid_dropped_never_widens(self):
        # A typo must not silently widen the traded set.
        assert normalize_option_types("CE,XX,FUT") == ["CE"]
        assert normalize_option_types("XX") == []

    def test_dedupe(self):
        assert normalize_option_types("CE,CE,PE") == ["CE", "PE"]

    def test_empty(self):
        assert normalize_option_types("") == []
        assert normalize_option_types(None) == []


class TestToDdmmmyy:
    def test_dashed(self):
        assert to_ddmmmyy("28-OCT-25") == "28OCT25"

    def test_iso(self):
        assert to_ddmmmyy("2025-10-28") == "28OCT25"

    def test_zero_padded_day(self):
        assert to_ddmmmyy("2025-10-07") == "07OCT25"

    def test_junk_is_none(self):
        assert to_ddmmmyy("not-a-date") is None
        assert to_ddmmmyy(None) is None


class TestIsMonthlyExpiry:
    # Three weeklies then the month-end contract, plus next month's.
    EXPIRIES = ["07-OCT-25", "14-OCT-25", "21-OCT-25", "28-OCT-25", "25-NOV-25"]

    def test_last_of_month_is_monthly(self):
        assert is_monthly_expiry("28-OCT-25", self.EXPIRIES) is True

    def test_mid_month_weekly_is_not(self):
        assert is_monthly_expiry("14-OCT-25", self.EXPIRIES) is False

    def test_other_month_last_is_monthly(self):
        assert is_monthly_expiry("25-NOV-25", self.EXPIRIES) is True

    def test_unparseable_is_false(self):
        assert is_monthly_expiry("junk", self.EXPIRIES) is False


class TestPickExpiry:
    EXPIRIES = ["07-OCT-25", "14-OCT-25", "21-OCT-25", "28-OCT-25", "25-NOV-25"]

    def test_weekly_picks_nearest(self):
        assert pick_expiry(self.EXPIRIES, (2025, 10, 1), "weekly") == "07-OCT-25"

    def test_monthly_skips_weeklies(self):
        assert pick_expiry(self.EXPIRIES, (2025, 10, 1), "monthly") == "28-OCT-25"

    def test_weekly_rolls_past_elapsed(self):
        assert pick_expiry(self.EXPIRIES, (2025, 10, 15), "weekly") == "21-OCT-25"

    def test_min_days_early_roll(self):
        # On 06-OCT with min_days=3, the 07-OCT weekly is inside the window.
        assert pick_expiry(self.EXPIRIES, (2025, 10, 6), "weekly", 3) == "14-OCT-25"

    def test_monthly_rolls_to_next_month(self):
        assert pick_expiry(self.EXPIRIES, (2025, 10, 29), "monthly") == "25-NOV-25"

    def test_empty(self):
        assert pick_expiry([], (2025, 10, 1), "weekly") is None


class TestSplitFutAndOpt:
    def test_splits_by_suffix(self):
        fut, opt = split_fut_and_opt(
            ["NIFTY28OCT25FUT", "NIFTY28OCT2525000CE", "NIFTY28OCT2525000PE"]
        )
        assert fut == ["NIFTY28OCT25FUT"]
        assert opt == ["NIFTY28OCT2525000CE", "NIFTY28OCT2525000PE"]

    def test_empty(self):
        assert split_fut_and_opt([]) == ([], [])


class TestMergeSymbols:
    def test_union_existing_first_deduped(self):
        assert merge_symbols(["NIFTY28OCT25FUT"], ["NIFTY28OCT2525000CE", "NIFTY28OCT25FUT"]) == [
            "NIFTY28OCT25FUT",
            "NIFTY28OCT2525000CE",
        ]

    def test_handles_empties(self):
        assert merge_symbols([], ["A"]) == ["A"]
        assert merge_symbols(["A"], []) == ["A"]


class TestResolveIndexOptionsEndToEnd:
    EXPIRIES = ["07-OCT-25", "28-OCT-25"]

    def test_full_ring_both_types(self):
        client = _FakeClient(self.EXPIRIES)
        out = resolve_index_options(
            client, "NIFTY", (2025, 10, 1), strikes=1, option_types=["CE", "PE"],
            expiry_kind="weekly", min_days=0,
        )
        # 3 offsets x 2 types, resolved on the nearest (weekly) expiry.
        assert out == [
            "NIFTY07OCT25ITM1CE",
            "NIFTY07OCT25ITM1PE",
            "NIFTY07OCT25ATMCE",
            "NIFTY07OCT25ATMPE",
            "NIFTY07OCT25OTM1CE",
            "NIFTY07OCT25OTM1PE",
        ]

    def test_monthly_kind_uses_month_end(self):
        client = _FakeClient(self.EXPIRIES)
        out = resolve_index_options(
            client, "NIFTY", (2025, 10, 1), strikes=0, option_types=["CE"],
            expiry_kind="monthly", min_days=0,
        )
        assert out == ["NIFTY28OCT25ATMCE"]

    def test_one_bad_strike_does_not_abort_the_rest(self):
        client = _FakeClient(self.EXPIRIES, fail_offsets={"ITM1"})
        out = resolve_index_options(
            client, "NIFTY", (2025, 10, 1), strikes=1, option_types=["CE"],
            expiry_kind="weekly", min_days=0,
        )
        assert out == ["NIFTY07OCT25ATMCE", "NIFTY07OCT25OTM1CE"]

    def test_no_expiries_returns_empty(self):
        client = _FakeClient([])
        assert (
            resolve_index_options(
                client, "NIFTY", (2025, 10, 1), 1, ["CE"], "weekly", 0
            )
            == []
        )

    def test_expiry_failure_is_contained(self):
        client = _FakeClient(self.EXPIRIES, expiry_raises=True)
        assert (
            resolve_index_options(
                client, "NIFTY", (2025, 10, 1), 1, ["CE"], "weekly", 0
            )
            == []
        )

    def test_underlying_quoted_on_index_exchange(self):
        client = _FakeClient(self.EXPIRIES)
        resolve_index_options(
            client, "BANKNIFTY", (2025, 10, 1), 0, ["CE"], "weekly", 0
        )
        # optionsymbol is called with the index base, not a constructed symbol.
        assert client.calls == [("BANKNIFTY", "07OCT25", "ATM", "CE")]
