"""NFO index near-month FUT contract resolver for OpenAlgo.

Turns index BASE names (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, ...)
into the current tradable NEAR-MONTH index-futures symbols on the NFO exchange
and writes them to the per-exchange screened watchlist file the scanning
strategies already consume:

    <watchlist-dir>/NFO.txt   (default: strategies/watchlists/NFO.txt)

Why this exists: NIFTY / BANKNIFTY themselves are INDICES (exchange NSE_INDEX,
quote-only) -- you cannot place an order on them. The tradable instrument is the
index FUTURE on NFO (e.g. NIFTY29MAY25FUT), which rolls every month, so a
hardcoded symbol goes stale. Like the MCX resolver, this rebuilds the live
near-month set each run so the strategies always trade the current contract
(with optional early roll before expiry via --min-days-to-expiry).

The scanning strategies read NFO.txt only when their WATCHLIST env var is unset
on NFO (env still wins), via strategies/watchlist_loader.load_watchlist. Run a
strategy instance with EXCHANGE=NFO and this resolver keeps its universe current.

Design mirrors strategies/mcx_resolver.py (same layered pure-core + I/O-shell
structure). The genuinely generic, well-tested date helpers (parse_expiry,
pick_front_month) and the FUT symbol builder are REUSED from mcx_resolver so the
tricky logic lives in exactly one place; only the NFO-specific discovery
(exchange="NFO", index spellings) is defined here.

OpenAlgo NFO FUT symbol format (see broker/zerodha/database/master_contract_db.py
line ~225): ``name + expiry.replace('-', '') + 'FUT'`` where expiry is formatted
``"%d-%b-%y"`` upper-cased, e.g. NIFTY + "29MAY25" + FUT -> ``NIFTY29MAY25FUT``.
Identical to the MCX FUT format, so the same builder applies. This resolver
PREFERS the concrete tradable symbol from the SDK's search results
(authoritative) and only FALLS BACK to string-building that format.

Run:
    uv run python strategies/nfo_resolver.py --user <id> \
        [--indices NIFTY,BANKNIFTY] [--indices-file path] \
        [--min-days-to-expiry 2] [--dry-run] [--allow-empty] \
        [--watchlist-dir strategies/watchlists] [--sleep 0.2]

The API key is read from OPENALGO_API_KEY, else looked up for --user from the
local database (like the /python strategy host does). No secret is ever logged.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Reuse the generic, offline-tested pure helpers from the MCX resolver so the
# hard date logic and the FUT symbol format live in exactly one place. These are
# exchange-agnostic (dates and string building), so importing them keeps NFO and
# MCX from drifting apart. The import triggers no side effects (mcx_resolver
# guards its main() behind __main__).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcx_resolver import (  # noqa: E402
    _response_list,
    build_mcx_symbol as build_fut_symbol,
    parse_expiry,
    pick_front_month,
)

IST = ZoneInfo("Asia/Kolkata")

# Default index set (the two the user asked for). STARTER set only; supply your
# own with --indices / --indices-file (e.g. add FINNIFTY, MIDCPNIFTY, NIFTYNXT50).
DEFAULT_INDICES = ["NIFTY", "BANKNIFTY"]

# Common human spellings mapped to the OpenAlgo/broker base name. Kept small and
# explicit -- only well-known aliases, so a typo never silently maps to a symbol.
_SPELLING_MAP = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "FIN NIFTY": "FINNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "MIDCAP NIFTY": "MIDCPNIFTY",
    "NIFTY MIDCAP": "MIDCPNIFTY",
    "MIDCPNIFTY 50": "MIDCPNIFTY",
    "NIFTY NEXT 50": "NIFTYNXT50",
    "NIFTY NEXT50": "NIFTYNXT50",
}


def log(message: str) -> None:
    """Plain-text timestamped logging (no secrets)."""
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# PURE CORE (no network, no SDK -- unit-tested offline)
# parse_expiry / pick_front_month / build_fut_symbol are imported (shared).
# ===============================================================================


def normalize_index_names(text) -> list[str]:
    """Parse a comma/newline list of index base names into a clean list.

    Splits on commas and newlines, strips, upper-cases, collapses internal
    whitespace, de-duplicates preserving first-seen order, drops blank lines and
    '#' comments, and maps well-known spellings (e.g. "BANK NIFTY" ->
    "BANKNIFTY") via ``_SPELLING_MAP``. After alias mapping any remaining
    internal spaces are removed, since index base names carry none.

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
        name = name.replace(" ", "")  # base names have no spaces after aliasing
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# ===============================================================================
# I/O SHELL (SDK discovery, CLI, atomic write) -- lazy SDK/DB imports
# ===============================================================================


def _expiry_strings(client, base: str) -> list[str] | None:
    """Fetch NFO FUT expiries for an index base name as a list of raw strings.

    Handles both a list-of-strings and a list-of-dicts (with an ``expiry`` key)
    payload. Any network/shape failure returns None (logged by the caller).
    """
    resp = client.expiry(symbol=base, exchange="NFO", instrumenttype="FUT")
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
    """Resolve the concrete tradable NFO index-FUT symbol for the chosen expiry.

    Searches NFO for the base name, keeps FUT instruments whose base matches and
    whose expiry parses to the same date as ``front_raw``, and returns the
    instrument's OpenAlgo ``symbol``. Returns None on any failure/no match.

    Anchoring: Kite search is a substring match, so "NIFTY" also returns
    BANKNIFTY, FINNIFTY, NIFTYNXT50, etc. Two tiers avoid trading the wrong
    index:
      1. exact instrument name == base (authoritative), else
      2. symbol prefix == base AND the next char is a DIGIT (the expiry day),
         so "NIFTY" does not match "NIFTYNXT50" (next char 'N', not a digit).
    BANKNIFTY never matches a "NIFTY" query since it does not start with NIFTY.
    """
    front_parsed = parse_expiry(front_raw)
    if front_parsed is None:
        return None
    resp = client.search(query=base, exchange="NFO")
    data = _response_list(resp)
    if not data:
        return None
    base_u = base.strip().upper()
    exact_match = None
    prefix_match = None
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("instrumenttype", "")).upper() != "FUT":
            continue
        sym = item.get("symbol")
        if not isinstance(sym, str) or not sym:
            continue
        if parse_expiry(item.get("expiry")) != front_parsed:
            continue
        name = str(item.get("name", "")).upper()
        sym_u = sym.upper()
        if name == base_u:
            exact_match = exact_match or sym  # first exact-name hit wins
        elif sym_u.startswith(base_u) and len(sym_u) > len(base_u) and sym_u[len(base_u)].isdigit():
            prefix_match = prefix_match or sym
    return exact_match or prefix_match


def resolve_index(client, base: str, today: tuple[int, int, int], min_days: int) -> str | None:
    """Resolve one index base to its current near-month tradable FUT symbol.

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
        log(f"{base}: no NFO FUT expiries returned - skipped")
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
        built = build_fut_symbol(base, front, day, month, year)
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


def load_indices(args) -> list[str]:
    """Resolve the index base list from CLI args (or the default set)."""
    if args.indices:
        return normalize_index_names(args.indices)
    if args.indices_file:
        text = Path(args.indices_file).read_text(encoding="utf-8")
        return normalize_index_names(text)
    log(
        f"No --indices/--indices-file given; using the {len(DEFAULT_INDICES)}-index "
        f"starter set ({', '.join(DEFAULT_INDICES)}). Supply your own for more."
    )
    return list(DEFAULT_INDICES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve NFO index base names to current near-month FUT symbols."
    )
    parser.add_argument("--user", help="User id for the DB API-key lookup (if no env key)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--indices", help="Comma-separated index base names (NIFTY,BANKNIFTY,...)")
    group.add_argument("--indices-file", help="File with one index base name per line")
    parser.add_argument(
        "--min-days-to-expiry",
        type=int,
        default=0,
        help="Skip a contract expiring within this many days (early roll); default 0",
    )
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds between API calls")
    parser.add_argument("--dry-run", action="store_true", help="Print result, do not write")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Write an empty watchlist when nothing resolves (default: refuse, "
        "to avoid silently disabling NFO trading on a transient API outage)",
    )
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

    indices = load_indices(args)
    if not indices:
        log("ERROR: empty index list - nothing to resolve")
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
        f"Resolving {len(indices)} NFO indices to near-month FUT "
        f"(min_days_to_expiry={min_days}): {', '.join(indices)}"
    )

    resolved: list[str] = []
    # Close the SDK client (and its HTTP/WS connections) on every path -- a
    # one-shot CLI, but leaking descriptors violates the close-on-all-paths rule.
    try:
        for i, base in enumerate(indices):
            symbol = resolve_index(client, base, today, min_days)
            if symbol:
                if symbol not in resolved:
                    resolved.append(symbol)
                    log(f"{base} -> {symbol}")
                else:
                    log(f"{base} -> {symbol} (duplicate, already listed)")
            if args.sleep > 0 and i < len(indices) - 1:
                time.sleep(args.sleep)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    log(f"Resolved {len(resolved)}/{len(indices)} indices to tradable FUT symbols")

    target = watchlist_file_path("NFO", args.watchlist_dir)
    if args.dry_run:
        log(f"[dry-run] would write {len(resolved)} symbols to {target} (not writing)")
        for s in resolved:
            log(f"  {s}")
        return 0

    # Refuse to overwrite a good watchlist with an empty one on a total
    # resolution failure: load_watchlist treats an existing NFO.txt as
    # authoritative even when empty, so writing [] after a transient API
    # outage would SILENTLY stop all NFO trading. A transient failure is
    # indistinguishable from a genuine "no live contracts", so require an
    # explicit --allow-empty to write an empty list.
    if not resolved and not args.allow_empty:
        log(
            f"Resolved 0 indices - NOT writing {target} (would disable NFO "
            f"trading). Existing watchlist left untouched. Pass --allow-empty to "
            f"override (e.g. a genuine no-contracts day)."
        )
        return 1

    header = [
        "OpenAlgo NFO index near-month FUT watchlist (auto-resolved)",
        f"generated {now.strftime('%Y-%m-%d %H:%M:%S')} IST",
        f"min_days_to_expiry={min_days} source=nfo_resolver",
        "one OpenAlgo symbol per line; consumed by strategies via load_watchlist",
    ]
    write_watchlist(target, resolved, header)
    log(f"Wrote {len(resolved)} symbols to {target}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
