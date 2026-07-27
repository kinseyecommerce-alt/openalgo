"""Offline unit tests for read_watchlists (the pure helper behind the
GET /python/api/watchlists route).

Covers: parsed symbols/count, the first header comment as the "generated"
note, dedupe/blank handling, exchange order preserved, missing exchanges
omitted, unreadable/empty files handled, and a hostile exchange sanitized.
No network, no Flask, no SDK.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from watchlist_loader import read_watchlists


class TestReadWatchlists:
    def test_reads_symbols_count_and_generated(self, tmp_path):
        (tmp_path / "NSE.txt").write_text(
            "# generated 2026-07-26 08:45:00 IST\nSBIN\n\nINFY\nsbin\n",
            encoding="utf-8",
        )
        (tmp_path / "MCX.txt").write_text(
            "# generated 2026-07-26 09:00:00 IST\nGOLD25AUGFUT\n",
            encoding="utf-8",
        )
        result = read_watchlists(tmp_path, ["NSE", "MCX"])

        assert [w["exchange"] for w in result] == ["NSE", "MCX"]

        nse = result[0]
        # Blank dropped, dupe collapsed, upper-cased.
        assert nse["symbols"] == ["SBIN", "INFY"]
        assert nse["count"] == 2
        assert nse["generated"] == "generated 2026-07-26 08:45:00 IST"
        assert nse["updated_at"] is not None

        mcx = result[1]
        assert mcx["symbols"] == ["GOLD25AUGFUT"]
        assert mcx["count"] == 1

    def test_missing_exchange_omitted(self, tmp_path):
        (tmp_path / "NSE.txt").write_text("SBIN\n", encoding="utf-8")
        result = read_watchlists(tmp_path, ["NSE", "BSE", "MCX"])
        assert [w["exchange"] for w in result] == ["NSE"]

    def test_no_header_comment_yields_none(self, tmp_path):
        (tmp_path / "NSE.txt").write_text("SBIN\nINFY\n", encoding="utf-8")
        result = read_watchlists(tmp_path, ["NSE"])
        assert result[0]["generated"] is None

    def test_empty_file_handled(self, tmp_path):
        # Only comments -> file exists, so it is included with zero symbols.
        (tmp_path / "NSE.txt").write_text("# only a comment\n\n", encoding="utf-8")
        result = read_watchlists(tmp_path, ["NSE"])
        assert len(result) == 1
        assert result[0]["symbols"] == []
        assert result[0]["count"] == 0
        assert result[0]["generated"] == "only a comment"

    def test_truly_empty_file_handled(self, tmp_path):
        (tmp_path / "NSE.txt").write_text("", encoding="utf-8")
        result = read_watchlists(tmp_path, ["NSE"])
        assert len(result) == 1
        assert result[0]["symbols"] == []
        assert result[0]["generated"] is None

    def test_order_preserved(self, tmp_path):
        for ex in ("NSE", "BSE", "MCX"):
            (tmp_path / f"{ex}.txt").write_text("SBIN\n", encoding="utf-8")
        result = read_watchlists(tmp_path, ["MCX", "NSE", "BSE"])
        assert [w["exchange"] for w in result] == ["MCX", "NSE", "BSE"]

    def test_bad_exchange_sanitized(self, tmp_path):
        # A hostile/malformed exchange resolves to the sanitized file name and
        # cannot escape the watchlist dir.
        (tmp_path / "NSE.txt").write_text("SBIN\n", encoding="utf-8")
        result = read_watchlists(tmp_path, ["n/se"])
        assert len(result) == 1
        assert result[0]["exchange"] == "NSE"
        assert result[0]["symbols"] == ["SBIN"]

    def test_missing_dir_is_safe(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        assert read_watchlists(missing, ["NSE", "MCX"]) == []

    def test_default_dir_when_none(self):
        # Passing None must not raise; the real strategies/watchlists dir may
        # or may not contain files, so just assert a list comes back.
        result = read_watchlists(None, ["NSE"])
        assert isinstance(result, list)
