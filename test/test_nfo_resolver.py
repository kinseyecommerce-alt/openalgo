"""Offline unit tests for the NFO index-futures resolver.

Covers the NFO-specific pure logic (normalize_index_names alias/dedupe/case) and
the search anchoring that keeps a "NIFTY" query from resolving to NIFTYNXT50 or
BANKNIFTY, plus an end-to-end resolve_index against a canned SDK client. The
generic date helpers (parse_expiry / pick_front_month / FUT symbol builder) are
imported from mcx_resolver and already covered by test_mcx_resolver; a couple of
NFO symbol-shape asserts are included for confidence. No network, no SDK.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from nfo_resolver import (  # noqa: E402
    _search_symbol,
    build_fut_symbol,
    normalize_index_names,
    resolve_index,
)


class _FakeClient:
    """Minimal stand-in for the openalgo SDK client.

    ``search`` does a substring match on ``name`` (mimicking Kite's ilike
    "%term%"), so a query for NIFTY also surfaces NIFTYNXT50 -- the collision the
    resolver must anchor against. ``expiry`` returns canned expiry strings.
    """

    def __init__(self, rows, expiries=None):
        self._rows = rows
        self._expiries = expiries or []

    def search(self, query=None, exchange=None):
        q = (query or "").upper()
        data = [r for r in self._rows if q in str(r.get("name", "")).upper()]
        return {"status": "success", "data": data}

    def expiry(self, symbol=None, exchange=None, instrumenttype=None):
        return {"status": "success", "data": list(self._expiries)}


class TestNormalizeIndexNames:
    def test_aliases_mapped(self):
        assert normalize_index_names("Nifty 50, Bank Nifty") == ["NIFTY", "BANKNIFTY"]

    def test_fin_and_midcap_aliases(self):
        assert normalize_index_names("fin nifty\nmidcap nifty\nnifty next 50") == [
            "FINNIFTY",
            "MIDCPNIFTY",
            "NIFTYNXT50",
        ]

    def test_dedupe_preserves_order(self):
        assert normalize_index_names("NIFTY, nifty 50, BANKNIFTY, NIFTY") == [
            "NIFTY",
            "BANKNIFTY",
        ]

    def test_comments_and_blanks_dropped(self):
        assert normalize_index_names("# indices\nNIFTY\n\n  \nBANKNIFTY") == [
            "NIFTY",
            "BANKNIFTY",
        ]

    def test_empty(self):
        assert normalize_index_names("") == []
        assert normalize_index_names(None) == []


class TestSearchSymbolAnchoring:
    # A "NIFTY" query returns NIFTY, NIFTYNXT50 (substring), and the resolver
    # must pick the exact-name NIFTY future, never NIFTYNXT50.
    ROWS = [
        {
            "name": "NIFTY",
            "symbol": "NIFTY29MAY25FUT",
            "expiry": "29-MAY-25",
            "instrumenttype": "FUT",
        },
        {
            "name": "NIFTYNXT50",
            "symbol": "NIFTYNXT5029MAY25FUT",
            "expiry": "29-MAY-25",
            "instrumenttype": "FUT",
        },
        {
            "name": "BANKNIFTY",
            "symbol": "BANKNIFTY29MAY25FUT",
            "expiry": "29-MAY-25",
            "instrumenttype": "FUT",
        },
    ]

    def test_nifty_resolves_exact_not_next50(self):
        client = _FakeClient(self.ROWS)
        assert _search_symbol(client, "NIFTY", "29-MAY-25") == "NIFTY29MAY25FUT"

    def test_banknifty_resolves_own(self):
        client = _FakeClient(self.ROWS)
        assert _search_symbol(client, "BANKNIFTY", "29-MAY-25") == "BANKNIFTY29MAY25FUT"

    def test_wrong_expiry_no_match(self):
        client = _FakeClient(self.ROWS)
        # No FUT for this expiry -> None (caller falls back to string-build).
        assert _search_symbol(client, "NIFTY", "26-JUN-25") is None

    def test_non_fut_ignored(self):
        rows = [
            {
                "name": "NIFTY",
                "symbol": "NIFTY29MAY2520000CE",
                "expiry": "29-MAY-25",
                "instrumenttype": "CE",
            }
        ]
        assert _search_symbol(_FakeClient(rows), "NIFTY", "29-MAY-25") is None


class TestBuildFutSymbol:
    def test_nifty_shape(self):
        # name + DDMMMYY + FUT, 2-digit year (matches master contract %y).
        assert build_fut_symbol("NIFTY", "29-MAY-25", 29, 5, 2025) == "NIFTY29MAY25FUT"

    def test_banknifty_shape(self):
        assert build_fut_symbol("BANKNIFTY", "26-JUN-25", 26, 6, 2025) == "BANKNIFTY26JUN25FUT"


class TestResolveIndexEndToEnd:
    ROWS = [
        {
            "name": "NIFTY",
            "symbol": "NIFTY26JUN25FUT",
            "expiry": "26-JUN-25",
            "instrumenttype": "FUT",
        },
        {
            "name": "NIFTY",
            "symbol": "NIFTY29MAY25FUT",
            "expiry": "29-MAY-25",
            "instrumenttype": "FUT",
        },
    ]

    def test_picks_front_month_via_search(self):
        # Two live NIFTY expiries; today before both -> nearest (29-MAY-25) wins.
        client = _FakeClient(self.ROWS, expiries=["29-MAY-25", "26-JUN-25"])
        out = resolve_index(client, "NIFTY", today=(2025, 5, 1), min_days=0)
        assert out == "NIFTY29MAY25FUT"

    def test_min_days_rolls_to_next(self):
        # With min_days=5 and today 27-MAY, the 29-MAY contract is inside the
        # window and is skipped -> rolls to 26-JUN.
        client = _FakeClient(self.ROWS, expiries=["29-MAY-25", "26-JUN-25"])
        out = resolve_index(client, "NIFTY", today=(2025, 5, 27), min_days=5)
        assert out == "NIFTY26JUN25FUT"

    def test_no_expiries_returns_none(self):
        client = _FakeClient(self.ROWS, expiries=[])
        assert resolve_index(client, "NIFTY", today=(2025, 5, 1), min_days=0) is None

    def test_search_miss_falls_back_to_build(self):
        # Expiry list has the front month, but search returns no matching FUT
        # (e.g. master contract lag) -> resolver string-builds the symbol.
        client = _FakeClient(rows=[], expiries=["29-MAY-25"])
        out = resolve_index(client, "NIFTY", today=(2025, 5, 1), min_days=0)
        assert out == "NIFTY29MAY25FUT"
