"""Offline unit tests for the shared watchlist loader.

Covers the documented precedence (env wins, including an empty env string ->
[]; then a screened file; then the NSE default / non-NSE empty), the
comma/newline/comment parser, the file-path builder, and safe handling of
missing directories and files. No network, no SDK.
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from watchlist_loader import load_watchlist, parse_symbol_list, watchlist_file_path

DEFAULT_NSE = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"]


class TestParseSymbolList:
    def test_comma_separated(self):
        assert parse_symbol_list("SBIN,INFY,TCS") == ["SBIN", "INFY", "TCS"]

    def test_newline_separated(self):
        assert parse_symbol_list("SBIN\nINFY\nTCS") == ["SBIN", "INFY", "TCS"]

    def test_mixed_comma_and_newline(self):
        assert parse_symbol_list("SBIN,INFY\nTCS") == ["SBIN", "INFY", "TCS"]

    def test_upper_and_strip(self):
        assert parse_symbol_list("  sbin , InFy ,tcs ") == ["SBIN", "INFY", "TCS"]

    def test_comments_and_blanks_ignored(self):
        text = "# header comment\n\nSBIN\n   \n# another\nINFY\n"
        assert parse_symbol_list(text) == ["SBIN", "INFY"]

    def test_dedupe_preserves_first_seen_order(self):
        assert parse_symbol_list("TCS,SBIN,tcs,INFY,sbin") == ["TCS", "SBIN", "INFY"]

    def test_none_and_empty(self):
        assert parse_symbol_list(None) == []
        assert parse_symbol_list("") == []
        assert parse_symbol_list("   ") == []
        assert parse_symbol_list(",, ,") == []


class TestWatchlistFilePath:
    def test_uses_upper_exchange(self, tmp_path):
        p = watchlist_file_path("nse", tmp_path)
        assert p == tmp_path / "NSE.txt"

    def test_default_dir_is_watchlists_next_to_module(self):
        p = watchlist_file_path("NSE")
        assert p.name == "NSE.txt"
        assert p.parent.name == "watchlists"
        assert p.parent.parent.name == "strategies"

    def test_strips_exchange(self, tmp_path):
        assert watchlist_file_path(" MCX ", tmp_path).name == "MCX.txt"

    def test_sanitizes_path_traversal(self, tmp_path):
        # A hostile/malformed exchange must not escape the watchlist dir.
        p = watchlist_file_path("../../etc/passwd", tmp_path)
        assert p.parent == tmp_path
        assert "/" not in p.name and ".." not in p.name

    def test_empty_exchange_is_unknown(self, tmp_path):
        assert watchlist_file_path("", tmp_path).name == "UNKNOWN.txt"


class TestLoadWatchlistPrecedence:
    def test_env_wins_over_file_and_default(self, tmp_path):
        # A file exists, but the env value takes precedence.
        (tmp_path / "NSE.txt").write_text("FILEA\nFILEB\n", encoding="utf-8")
        result = load_watchlist("NSE", DEFAULT_NSE, "SBIN,INFY", tmp_path)
        assert result == ["SBIN", "INFY"]

    def test_empty_env_string_yields_empty_list(self, tmp_path):
        # Explicit empty string is a deliberate "scan nothing" -- env still wins,
        # even over an existing file and the default.
        (tmp_path / "NSE.txt").write_text("FILEA\n", encoding="utf-8")
        assert load_watchlist("NSE", DEFAULT_NSE, "", tmp_path) == []

    def test_file_used_when_env_unset(self, tmp_path):
        (tmp_path / "NSE.txt").write_text("# screened\nfilea\nFILEB\nfilea\n", encoding="utf-8")
        assert load_watchlist("NSE", DEFAULT_NSE, None, tmp_path) == ["FILEA", "FILEB"]

    def test_existing_empty_file_is_authoritative(self, tmp_path):
        # A file that parses to nothing (only comments) yields [] -- it does not
        # fall through to the default.
        (tmp_path / "NSE.txt").write_text("# only a comment\n\n", encoding="utf-8")
        assert load_watchlist("NSE", DEFAULT_NSE, None, tmp_path) == []

    def test_default_nse_when_no_env_no_file(self, tmp_path):
        assert load_watchlist("NSE", DEFAULT_NSE, None, tmp_path) == DEFAULT_NSE

    def test_non_nse_empty_when_unset(self, tmp_path):
        assert load_watchlist("MCX", DEFAULT_NSE, None, tmp_path) == []
        assert load_watchlist("NFO", DEFAULT_NSE, None, tmp_path) == []

    def test_bse_equity_uses_default_when_unset(self, tmp_path):
        # BSE is an equity cash exchange: the large-cap default list is valid
        # there, so an unconfigured BSE strategy must still scan (backward
        # compatible with the pre-loader four_ema behavior), unlike MCX/NFO.
        assert load_watchlist("BSE", DEFAULT_NSE, None, tmp_path) == DEFAULT_NSE
        assert load_watchlist(" bse ", DEFAULT_NSE, None, tmp_path) == DEFAULT_NSE

    def test_non_nse_still_honors_env(self, tmp_path):
        assert load_watchlist("MCX", DEFAULT_NSE, "CRUDEOIL25JULFUT", tmp_path) == [
            "CRUDEOIL25JULFUT"
        ]

    def test_non_nse_uses_its_own_file(self, tmp_path):
        (tmp_path / "MCX.txt").write_text("GOLD25AUGFUT\n", encoding="utf-8")
        assert load_watchlist("MCX", DEFAULT_NSE, None, tmp_path) == ["GOLD25AUGFUT"]

    def test_missing_dir_is_safe(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        assert load_watchlist("NSE", DEFAULT_NSE, None, missing) == DEFAULT_NSE
        assert load_watchlist("MCX", DEFAULT_NSE, None, missing) == []

    def test_default_nse_is_normalized(self, tmp_path):
        assert load_watchlist("NSE", [" sbin ", "infy", "SBIN"], None, tmp_path) == [
            "SBIN",
            "INFY",
        ]
