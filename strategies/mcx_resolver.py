"""MCX commodity near-month FUT contract resolver for OpenAlgo.

Turns BASE commodity names (CRUDEOIL, GOLDM, SILVERM, NATURALGAS, COPPER, ...)
into the current tradable NEAR-MONTH futures symbols on the MCX exchange and
writes them to the per-exchange screened watchlist file the scanning strategies
already consume:

    <watchlist-dir>/MCX.txt   (default: strategies/watchlists/MCX.txt)

The scanning strategies read that file only when their WATCHLIST env var is
unset on MCX (env still wins), via strategies/watchlist_loader.load_watchlist.
MCX futures roll every month, so a hardcoded symbol like CRUDEOIL20MAY24FUT goes
stale monthly; this resolver rebuilds the live near-month set each run so the
strategies always trade the current contract (with optional early roll before
expiry via --min-days-to-expiry).

Layers:
  - PURE core (no network, unit-tested offline):
        parse_expiry(expiry_str)                 -> (year, month, day) | None
        pick_front_month(expiries, today, ...)   -> raw expiry str | None
        build_mcx_symbol(base, expiry_str, d, m, y) -> str
        normalize_base_names(text)               -> list[str]
  - I/O shell (SDK discovery, CLI, atomic file write). The SDK and the database
    are imported LAZILY so the pure core imports with no dependencies.

OpenAlgo MCX FUT symbol format (see CLAUDE.md and
broker/zerodha/database/master_contract_db.py): the master contract builds a
FUT symbol as ``name + expiry.replace('-', '') + 'FUT'`` where expiry is
formatted ``"%d-%b-%y"`` upper-cased (e.g. "20-MAY-24"), giving
``CRUDEOILM20MAY24FUT``. This resolver PREFERS to read the concrete tradable
symbol straight from the SDK's search results (authoritative), and only
FALLS BACK to string-building that documented format when search does not
return a usable symbol.

Run:
    uv run python strategies/mcx_resolver.py --user <id> \
        [--commodities CRUDEOIL,GOLDM,...] [--commodities-file path] \
        [--min-days-to-expiry 2] [--dry-run] \
        [--watchlist-dir strategies/watchlists] [--sleep 0.2]

The API key is read from OPENALGO_API_KEY, else looked up for --user from the
local database (like the /python strategy host does). No secret is ever logged.
"""

from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Default commodity set (the user's uploaded list). This is a STARTER set only;
# supply your own with --commodities / --commodities-file.
DEFAULT_COMMODITIES = ["CRUDEOIL", "NATURALGAS", "GOLDM", "SILVERM", "COPPER"]

# Common human spellings mapped to the OpenAlgo/broker base name. Kept small and
# explicit -- only well-known aliases, so a typo never silently maps to a symbol.
_SPELLING_MAP = {
    "NATURAL GAS": "NATURALGAS",
    "NATURALGAS MINI": "NATURALGASM",
    "NATURAL GAS MINI": "NATURALGASM",
    "CRUDE OIL": "CRUDEOIL",
    "CRUDE OIL MINI": "CRUDEOILM",
    "CRUDEOIL MINI": "CRUDEOILM",
    "GOLD MINI": "GOLDM",
    "SILVER MINI": "SILVERM",
    "GOLD GUINEA": "GOLDGUINEA",
}

# Month number (1-12) -> uppercase 3-letter abbreviation ("JAN".."DEC").
_MONTH_ABBR = [calendar.month_abbr[m].upper() for m in range(13)]  # index 0 unused


def log(message: str) -> None:
    """Plain-text timestamped logging (no secrets)."""
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# PURE CORE (no network, no SDK -- unit-tested offline)
# ===============================================================================

# strptime patterns tried in order. Covers the formats OpenAlgo/Kite emit:
# "26-AUG-25", "26-AUG-2025", "2025-08-26", "28AUG25", "28AUG2025". %b matches
# month abbreviations case-insensitively in CPython.
_EXPIRY_FORMATS = (
    "%d-%b-%y",
    "%d-%b-%Y",
    "%Y-%m-%d",
    "%d%b%y",
    "%d%b%Y",
    "%d-%m-%Y",
)


def parse_expiry(expiry_str) -> tuple[int, int, int] | None:
    """Parse an expiry string into a ``(year, month, day)`` tuple.

    Robustly handles the formats OpenAlgo/Kite use, e.g. "26-AUG-25",
    "2025-08-26", "28AUG25", "26-AUG-2025".

    Args:
        expiry_str: The raw expiry string (or anything).

    Returns:
        ``(year, month, day)`` on success, or None for empty/None/unparseable
        input.
    """
    if not isinstance(expiry_str, str):
        return None
    text = expiry_str.strip().upper()
    if not text:
        return None
    for fmt in _EXPIRY_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return (dt.year, dt.month, dt.day)
    return None


def pick_front_month(
    expiries: list[str], today: tuple[int, int, int], min_days: int = 0
) -> str | None:
    """Choose the NEAREST expiry that is at least ``min_days`` away from today.

    From a list of raw expiry strings, keep the parseable ones, drop any whose
    date is before ``today + min_days``, and return the RAW string of the
    nearest remaining expiry (so the caller can match it back to the concrete
    instrument). ``min_days`` lets a contract that is about to expire be skipped
    so the near month auto-rolls early.

    Args:
        expiries: Raw expiry strings (mixed formats tolerated).
        today: Reference date as ``(year, month, day)``.
        min_days: Minimum whole days from today the chosen expiry must be
            (``>= today + min_days``); 0 means today itself qualifies.

    Returns:
        The chosen raw expiry string, or None when nothing qualifies.
    """
    if not expiries:
        return None
    try:
        today_date = date(today[0], today[1], today[2])
    except (TypeError, ValueError, IndexError):
        return None
    threshold = today_date + timedelta(days=max(0, min_days))

    best_raw: str | None = None
    best_date: date | None = None
    for raw in expiries:
        parsed = parse_expiry(raw)
        if parsed is None:
            continue
        try:
            d = date(parsed[0], parsed[1], parsed[2])
        except ValueError:
            continue
        if d < threshold:
            continue
        if best_date is None or d < best_date:
            best_date = d
            best_raw = raw
    return best_raw


def build_mcx_symbol(base: str, expiry_str, day: int, month: int, year: int) -> str:
    """Build the OpenAlgo MCX FUT symbol from expiry components (FALLBACK path).

    Mirrors broker/zerodha/database/master_contract_db.py, which builds a FUT
    symbol as ``name + expiry.replace('-', '') + 'FUT'`` with expiry formatted
    ``"%d-%b-%y"`` upper (zero-padded day, uppercase 3-letter month, 2-digit
    year). So base=CRUDEOILM, day=20, month=5, year=2024 -> CRUDEOILM20MAY24FUT.

    Prefer resolving the concrete tradable symbol from the SDK's search results
    instead; use this only when search returns nothing usable. The ``day``,
    ``month`` and ``year`` components are authoritative here; ``expiry_str`` is
    accepted for symmetry with the caller and is not required to build.

    Args:
        base: Base commodity name (spaces stripped, upper-cased).
        expiry_str: The raw expiry string (unused for construction; kept for the
            caller's convenience/logging).
        day: Day of month (1-31).
        month: Month number (1-12).
        year: Full year (e.g. 2024) or 2-digit (e.g. 24); only the last 2 digits
            are used, matching the master contract's ``%y``.

    Returns:
        The constructed OpenAlgo MCX FUT symbol.

    Raises:
        ValueError: If ``month`` is out of the 1-12 range.
    """
    del expiry_str  # components are authoritative; documented for the caller
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    clean_base = "".join((base or "").split()).upper()
    part = f"{int(day):02d}{_MONTH_ABBR[month]}{int(year) % 100:02d}"
    return f"{clean_base}{part}FUT"


def normalize_base_names(text) -> list[str]:
    """Parse a comma/newline list of base commodity names into a clean list.

    Splits on commas and newlines, strips, upper-cases, de-duplicates preserving
    first-seen order, drops blank lines and '#' comments, and maps well-known
    spellings (e.g. "NATURAL GAS" -> "NATURALGAS") via ``_SPELLING_MAP``.

    Args:
        text: Raw text (or None).

    Returns:
        The cleaned, de-duplicated list of base names ([] for None/empty).
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for chunk in text.replace(",", "\n").split("\n"):
        name = chunk.strip()
        if not name or name.startswith("#"):
            continue
        name = " ".join(name.upper().split())  # collapse internal whitespace
        name = _SPELLING_MAP.get(name, name)
        # After alias mapping, a plain base name carries no spaces.
        name = name.replace(" ", "")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# ===============================================================================
# I/O SHELL (SDK discovery, CLI, atomic write) -- lazy SDK/DB imports
# ===============================================================================


def _response_list(resp) -> list | None:
    """Extract a list payload from an SDK response, defensively.

    Accepts either a bare list, or a dict with a ``data`` list (the OpenAlgo
    ``{"status": ..., "data": [...]}`` envelope). Returns None for an error
    envelope or any unexpected shape (caller treats None as "unresolved, skip").
    """
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        if str(resp.get("status", "success")).lower() == "error":
            return None
        data = resp.get("data")
        if isinstance(data, list):
            return data
        return None
    return None


def _expiry_strings(client, base: str) -> list[str] | None:
    """Fetch MCX FUT expiries for a base name as a list of raw strings.

    Handles both a list-of-strings and a list-of-dicts (with an ``expiry`` key)
    payload. Any network/shape failure returns None (logged by the caller).
    """
    resp = client.expiry(symbol=base, exchange="MCX", instrumenttype="FUT")
    data = _response_list(resp)
    if data is None:
        return None
    out: list[str] = []
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            val = item.get("expiry")
            if isinstance(val, str):
                out.append(val)
    return out


def _search_symbol(client, base: str, front_raw: str) -> str | None:
    """Resolve the concrete tradable MCX FUT symbol for the chosen expiry.

    Searches MCX for the base name, keeps FUT instruments whose base matches and
    whose expiry parses to the same date as ``front_raw``, and returns the
    instrument's OpenAlgo ``symbol``. Returns None on any failure/no match.
    """
    front_parsed = parse_expiry(front_raw)
    if front_parsed is None:
        return None
    resp = client.search(query=base, exchange="MCX")
    data = _response_list(resp)
    if not data:
        return None
    base_u = base.strip().upper()
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("instrumenttype", "")).upper() != "FUT":
            continue
        sym = item.get("symbol")
        if not isinstance(sym, str) or not sym:
            continue
        name = str(item.get("name", "")).upper()
        # Match the base either by the instrument name or by the symbol prefix.
        if base_u not in (name, "") and not sym.upper().startswith(base_u):
            continue
        if parse_expiry(item.get("expiry")) != front_parsed:
            continue
        return sym
    return None


def resolve_commodity(client, base: str, today: tuple[int, int, int], min_days: int) -> str | None:
    """Resolve one base commodity to its current near-month tradable FUT symbol.

    Strategy: list expiries via the SDK, pick the front month (respecting
    ``min_days`` for early roll), then resolve the concrete tradable symbol from
    search (authoritative); fall back to string-building the documented master
    contract format if search yields nothing. Every network call is guarded --
    any failure logs a warning and returns None (the run never crashes).
    """
    try:
        expiries = _expiry_strings(client, base)
    except Exception as e:
        log(f"{base}: expiry lookup failed ({type(e).__name__}: {e}) - skipped")
        return None
    if not expiries:
        log(f"{base}: no MCX FUT expiries returned - skipped")
        return None

    front = pick_front_month(expiries, today, min_days)
    if front is None:
        log(f"{base}: no expiry >= today+{min_days}d among {len(expiries)} found - skipped")
        return None

    try:
        symbol = _search_symbol(client, base, front)
    except Exception as e:
        log(f"{base}: search failed ({type(e).__name__}: {e}); will try string-build")
        symbol = None

    if symbol:
        return symbol

    parsed = parse_expiry(front)
    if parsed is None:
        log(f"{base}: front expiry {front!r} unparseable for fallback build - skipped")
        return None
    year, month, day = parsed
    try:
        built = build_mcx_symbol(base, front, day, month, year)
    except ValueError as e:
        log(f"{base}: fallback build failed ({e}) - skipped")
        return None
    log(f"{base}: search did not return a symbol; built {built} from expiry {front}")
    return built


def resolve_api_key(user: str | None) -> str | None:
    """Resolve the API key: OPENALGO_API_KEY env, else the local DB for --user.

    The database import is wrapped so the pure/offline path never needs Flask or
    the SDK. The key is returned to the caller and NEVER logged.
    """
    key = os.getenv("OPENALGO_API_KEY")
    if key:
        return key
    if not user:
        return None
    try:
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from database.auth_db import get_api_key_for_tradingview

        return get_api_key_for_tradingview(user)
    except Exception as e:
        log(f"API key lookup for user failed ({type(e).__name__}); set OPENALGO_API_KEY: {e}")
        return None


def load_commodities(args) -> list[str]:
    """Resolve the base commodity list from CLI args (or the default set)."""
    if args.commodities:
        return normalize_base_names(args.commodities)
    if args.commodities_file:
        text = Path(args.commodities_file).read_text(encoding="utf-8")
        return normalize_base_names(text)
    log(
        f"No --commodities/--commodities-file given; using the "
        f"{len(DEFAULT_COMMODITIES)}-commodity starter set "
        f"({', '.join(DEFAULT_COMMODITIES)}). Supply your own for a real universe."
    )
    return list(DEFAULT_COMMODITIES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve MCX commodity base names to current near-month FUT symbols."
    )
    parser.add_argument("--user", help="User id for the DB API-key lookup (if no env key)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--commodities", help="Comma-separated base names (CRUDEOIL,GOLDM,...)")
    group.add_argument("--commodities-file", help="File with one base commodity name per line")
    parser.add_argument(
        "--min-days-to-expiry",
        type=int,
        default=0,
        help="Skip a contract expiring within this many days (early roll); default 0",
    )
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds between API calls")
    parser.add_argument("--dry-run", action="store_true", help="Print result, do not write")
    parser.add_argument("--watchlist-dir", help="Output dir (default strategies/watchlists)")
    return parser


def main(argv: list[str] | None = None) -> int:
    import time

    # Make the sibling modules importable however main() is invoked.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from screener import write_watchlist  # reuse the atomic tmp+fsync+replace writer
    from watchlist_loader import watchlist_file_path

    args = build_parser().parse_args(argv)
    min_days = max(0, args.min_days_to_expiry)

    commodities = load_commodities(args)
    if not commodities:
        log("ERROR: empty commodity list - nothing to resolve")
        return 1

    api_key = resolve_api_key(args.user)
    if not api_key:
        log("ERROR: no API key (set OPENALGO_API_KEY or pass --user with a stored key)")
        return 1

    host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
    ws_url = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")
    from openalgo import api  # lazy import: pure core stays SDK-free

    client = api(api_key=api_key, host=host, ws_url=ws_url)

    now = datetime.now(IST)
    today = (now.year, now.month, now.day)
    log(
        f"Resolving {len(commodities)} MCX commodities to near-month FUT "
        f"(min_days_to_expiry={min_days}): {', '.join(commodities)}"
    )

    resolved: list[str] = []
    # Close the SDK client (and its HTTP/WS connections) on every path -- a
    # one-shot CLI, but leaking descriptors violates the close-on-all-paths rule.
    try:
        for i, base in enumerate(commodities):
            symbol = resolve_commodity(client, base, today, min_days)
            if symbol:
                if symbol not in resolved:
                    resolved.append(symbol)
                    log(f"{base} -> {symbol}")
                else:
                    log(f"{base} -> {symbol} (duplicate, already listed)")
            if args.sleep > 0 and i < len(commodities) - 1:
                time.sleep(args.sleep)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    log(f"Resolved {len(resolved)}/{len(commodities)} commodities to tradable FUT symbols")

    target = watchlist_file_path("MCX", args.watchlist_dir)
    if args.dry_run:
        log(f"[dry-run] would write {len(resolved)} symbols to {target} (not writing)")
        for s in resolved:
            log(f"  {s}")
        return 0

    header = [
        "OpenAlgo MCX near-month FUT watchlist (auto-resolved)",
        f"generated {now.strftime('%Y-%m-%d %H:%M:%S')} IST",
        f"min_days_to_expiry={min_days} source=mcx_resolver",
        "one OpenAlgo symbol per line; consumed by strategies via load_watchlist",
    ]
    write_watchlist(target, resolved, header)
    log(f"Wrote {len(resolved)} symbols to {target}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
