"""Shared watchlist loader for OpenAlgo's scanning strategies.

A single, pure, import-safe helper (no network, no side effects) that resolves
the list of symbols a scanning strategy should trade, with this PRECEDENCE:

    1. env_value is not None
       -> parse it as a symbol list (comma/newline split, upper/strip, dedupe
          preserving order). This preserves 100 percent backward compatibility
          with the existing WATCHLIST env behavior, INCLUDING that an explicit
          empty env string ("") yields an empty list [] (scan nothing).

    2. else a screened file exists at
       <watchlist_dir or strategies/watchlists>/<EXCHANGE>.txt
       -> read it (one symbol per line; blank lines and lines starting with '#'
          are ignored; upper/strip/dedupe). The file is AUTHORITATIVE when it
          exists: a file that parses to no symbols yields [] (the screener wrote
          an empty universe), it does NOT fall through to the default.

    3. else (no env, no file)
       -> for an EQUITY CASH exchange (NSE, BSE) return default_nse
          (normalized -- those large-cap symbols are valid on both); for any
          other exchange (MCX, NFO, BFO, CDS, NCDEX, ...) return [] -- the same
          "no guessing expiry symbols" rule the intraday strategies already
          follow, since derivative/commodity symbols carry expiries and there
          is no safe hardcoded default.

The screened file is produced by strategies/screener.py (the pre-market
liquidity + volatility screener).
"""

from __future__ import annotations

from pathlib import Path

# Directory (relative to this module, i.e. strategies/) that holds the
# per-exchange screened watchlist files when no explicit dir is given.
_DEFAULT_DIRNAME = "watchlists"

# Equity cash exchanges whose large-cap default list is valid. Non-NSE/BSE
# exchanges (MCX, NFO, BFO, CDS, NCDEX, ...) carry expiry/commodity symbols
# with no safe hardcoded default, so they scan nothing when unconfigured.
_EQUITY_CASH_EXCHANGES = frozenset({"NSE", "BSE"})


def parse_symbol_list(text: str | None) -> list[str]:
    """Parse a symbol blob into a clean list.

    Accepts comma- and/or newline-separated symbols (so it handles both the
    WATCHLIST env format and the one-symbol-per-line file format). Symbols are
    stripped, upper-cased and de-duplicated preserving first-seen order; blank
    entries and lines beginning with '#' (comments) are dropped.

    Args:
        text: The raw text, or None.

    Returns:
        The cleaned, de-duplicated list of symbols ([] for None/empty).
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in text.replace(",", "\n").split("\n"):
        symbol = chunk.strip()
        if not symbol or symbol.startswith("#"):
            continue
        symbol = symbol.upper()
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def watchlist_file_path(exchange: str, watchlist_dir: str | Path | None = None) -> Path:
    """Build the screened-watchlist file path for an exchange.

    Args:
        exchange: OpenAlgo exchange code (e.g. "NSE", "MCX"). Case-insensitive;
            the file is named by the upper-cased code.
        watchlist_dir: Directory holding the files; defaults to the
            ``watchlists`` directory next to this module (strategies/watchlists).

    Returns:
        Path to ``<dir>/<EXCHANGE>.txt`` (not guaranteed to exist).
    """
    if watchlist_dir is not None:
        base = Path(watchlist_dir)
    else:
        base = Path(__file__).resolve().parent / _DEFAULT_DIRNAME
    # Sanitize the exchange to a bare alphanumeric token so a hostile or
    # malformed value (e.g. "../../etc/passwd") cannot escape the directory --
    # real OpenAlgo exchange codes are alphanumeric (NSE, BSE, NFO, MCX, ...).
    safe = "".join(ch for ch in (exchange or "").strip().upper() if ch.isalnum())
    if not safe:
        safe = "UNKNOWN"
    return base / f"{safe}.txt"


def load_watchlist(
    exchange: str,
    default_nse: list[str],
    env_value: str | None,
    watchlist_dir: str | Path | None = None,
) -> list[str]:
    """Resolve the watchlist for a scanning strategy (see module docstring).

    Args:
        exchange: OpenAlgo exchange code (e.g. "NSE", "MCX").
        default_nse: The fallback symbol list used for the equity cash
            exchanges (NSE, BSE) when neither an env value nor a screened file
            is present.
        env_value: The raw WATCHLIST env value, or None when the env var is
            unset. An explicit empty string yields [] (env wins).
        watchlist_dir: Optional override for the screened-file directory.

    Returns:
        The resolved list of OpenAlgo symbols (possibly empty).
    """
    # 1. Env wins (including "" -> []), preserving current behavior exactly.
    if env_value is not None:
        return parse_symbol_list(env_value)

    # 2. Screened file (authoritative when it exists).
    path = watchlist_file_path(exchange, watchlist_dir)
    try:
        if path.is_file():
            return parse_symbol_list(path.read_text(encoding="utf-8"))
    except OSError:
        # Unreadable file -> fall through to the default rule, never raise.
        pass

    # 3. Default: equity cash exchanges (NSE, BSE) get the fallback list;
    #    derivative/commodity exchanges scan nothing (no safe expiry default).
    if (exchange or "").strip().upper() in _EQUITY_CASH_EXCHANGES:
        return parse_symbol_list(",".join(default_nse))
    return []
