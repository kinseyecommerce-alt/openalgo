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

from collections.abc import Iterable
from datetime import datetime
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


def read_watchlists(
    watchlist_dir: str | Path | None,
    exchanges: Iterable[str],
) -> list[dict]:
    """Read the screened watchlist files for a set of exchanges (pure, no Flask).

    For each exchange whose ``<EXCHANGE>.txt`` file exists (located exactly as
    :func:`watchlist_file_path` does, so path traversal is sanitized away),
    parse its symbols via :func:`parse_symbol_list`, capture the first
    ``#``-comment line as a human "generated" note (leading ``#`` and
    whitespace stripped), and record the file's modification time as an
    ISO-8601 string. Exchanges with no file -- or an unreadable/empty-path
    file -- are omitted. This function never raises; an unreadable file is
    simply skipped.

    Args:
        watchlist_dir: Directory holding the per-exchange files; ``None`` uses
            the default ``watchlists`` directory next to this module.
        exchanges: Exchange codes to check, in the order to return them
            (e.g. ``["NSE", "BSE", "MCX", "NFO", "CDS"]``).

    Returns:
        A list of dicts (existing files only, in ``exchanges`` order)::

            {"exchange": "NSE", "symbols": [...], "count": N,
             "generated": "<first header comment or None>",
             "updated_at": "<file mtime ISO-8601 or None>"}
    """
    out: list[dict] = []
    for exchange in exchanges:
        path = watchlist_file_path(exchange, watchlist_dir)
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            # Unreadable file -> skip it, never raise.
            continue

        symbols = parse_symbol_list(text)

        generated: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                generated = stripped.lstrip("#").strip() or None
                break

        updated_at: str | None = None
        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        except OSError:
            updated_at = None

        out.append(
            {
                "exchange": path.stem,
                "symbols": symbols,
                "count": len(symbols),
                "generated": generated,
                "updated_at": updated_at,
            }
        )
    return out


# Exchange -> the resolver that writes its screened watchlist file. Derivative
# and commodity symbols carry expiries, so there is no safe hardcoded default;
# these resolvers rebuild the live near-month set (see strategies/README.md).
_RESOLVER_FOR_EXCHANGE = {
    "MCX": ("strategies/mcx_resolver.py", "CRUDEOIL<expiry>FUT"),
    "NFO": ("strategies/nfo_resolver.py", "NIFTY<expiry>FUT"),
    "BFO": ("strategies/nfo_resolver.py", "SENSEX<expiry>FUT"),
}


def empty_watchlist_warning(
    exchange: str,
    env_value: str | None,
    symbols: list[str],
    watchlist_dir: str | Path | None = None,
) -> str | None:
    """Explain an empty resolved watchlist, or return None when there is one.

    A scanning strategy whose watchlist resolves to nothing scans NOTHING and
    would otherwise sit silently idle looking healthy. This builds the operator-
    facing warning describing WHY it is empty and the concrete next step, so the
    silence is never mistaken for "no setups today".

    Pure: no file writes, no network. It may stat the screened file only to say
    whether one exists.

    Args:
        exchange: OpenAlgo exchange code the strategy trades (e.g. "NSE", "NFO").
        env_value: The raw ``WATCHLIST`` env value, or None when unset.
        symbols: The watchlist as resolved by :func:`load_watchlist`.
        watchlist_dir: Optional override for the screened-file directory.

    Returns:
        The warning string, or None when ``symbols`` is non-empty (nothing to
        warn about).
    """
    if symbols:
        return None

    code = (exchange or "").strip().upper()

    # An explicitly empty env value is a deliberate "scan nothing" -- report it
    # as intentional rather than implying a misconfiguration.
    if env_value is not None:
        return (
            f"WARNING: WATCHLIST is set but resolves to no symbols on {code}. "
            f"Scanning NOTHING. Unset WATCHLIST to fall back to the screened "
            f"watchlist file, or set it to real symbols to trade."
        )

    path = watchlist_file_path(code, watchlist_dir)
    try:
        file_exists = path.is_file()
    except OSError:
        file_exists = False

    if file_exists:
        # The file is authoritative even when empty, so an empty one silently
        # disables trading -- the most confusing case of all.
        return (
            f"WARNING: the screened watchlist {path} exists but contains no "
            f"symbols. Scanning NOTHING. It is authoritative when present, so "
            f"an empty file disables trading on {code}. Re-run the screener/"
            f"resolver for {code}, or set WATCHLIST to override it."
        )

    resolver = _RESOLVER_FOR_EXCHANGE.get(code)
    if resolver is not None:
        script, example = resolver
        return (
            f"WARNING: EXCHANGE={code} with no WATCHLIST set and no {path.name} - "
            f"{code} symbols carry expiries so there is NO safe default watchlist. "
            f"Scanning NOTHING. Run `uv run python {script}` to resolve the current "
            f"near-month contracts, or set WATCHLIST explicitly (e.g. {example})."
        )

    return (
        f"WARNING: no symbols resolved for EXCHANGE={code} and no {path.name}. "
        f"Scanning NOTHING. {code} symbols may carry expiries, so there is no safe "
        f"default. Set WATCHLIST explicitly to trade."
    )


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
