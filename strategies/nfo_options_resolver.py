"""NFO index OPTIONS (CE/PE) strike resolver for OpenAlgo.

Resolves a ring of strikes around the money -- ATM plus N in-the-money and N
out-of-the-money -- for CE and/or PE on the chosen weekly or monthly expiry, and
writes the tradable option symbols to the per-exchange screened watchlist file
the scanning strategies already consume:

    <watchlist-dir>/NFO.txt   (default: strategies/watchlists/NFO.txt)

Companion to strategies/nfo_resolver.py (index FUTURES). Options roll weekly and
carry strikes that move with spot, so a hardcoded symbol goes stale within days;
this rebuilds the live set each run.

Symbol resolution is AUTHORITATIVE, not string-built: it calls the SDK's
``optionsymbol(underlying, exchange, offset, option_type, expiry_date)``, which
resolves ATM from live spot server-side and returns the exact tradable symbol
(and lot size). That avoids guessing the per-index strike interval, and avoids
the ``format_strike`` decimal rules in the master contract.

Offsets: the SDK expresses strikes relative to the money -- ``ATM``, ``ITM1..N``,
``OTM1..N``. This is direction-correct for both option types automatically (for a
CE, OTM is a HIGHER strike; for a PE, OTM is a LOWER strike), so ``--strikes N``
means "N either side of ATM" for whichever type is being resolved.

    --strikes 2 --option-types CE,PE  ->  ITM2 ITM1 ATM OTM1 OTM2  x  {CE,PE}
                                          = 10 symbols

NFO.txt is SHARED with the futures resolver (one watchlist file per exchange).
By default this REPLACES the file; pass ``--merge`` to union with what is already
there (e.g. keep the index futures and add options). The run refuses to clobber a
non-empty file containing FUT symbols unless ``--merge`` or ``--replace`` is
given explicitly, so an options run never silently disables futures trading.

Run:
    uv run python strategies/nfo_options_resolver.py --user <id> \
        [--indices NIFTY,BANKNIFTY] [--strikes 2] [--option-types CE,PE] \
        [--expiry-kind weekly|monthly] [--min-days-to-expiry 0] \
        [--merge | --replace] [--dry-run] [--allow-empty] \
        [--watchlist-dir strategies/watchlists] [--sleep 0.2]

The API key is read from OPENALGO_API_KEY, else looked up for --user from the
local database. No secret is ever logged.
"""

from __future__ import annotations

import argparse
import calendar
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Reuse the generic, offline-tested helpers so the hard date logic lives in one
# place (see nfo_resolver for the same rationale). No side effects on import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcx_resolver import _response_list, parse_expiry, pick_front_month  # noqa: E402
from nfo_resolver import normalize_index_names  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_INDICES = ["NIFTY", "BANKNIFTY"]
DEFAULT_STRIKES = 2
DEFAULT_OPTION_TYPES = ["CE", "PE"]

# Month number (1-12) -> uppercase 3-letter abbreviation, for the DDMMMYY the
# optionsymbol API expects.
_MONTH_ABBR = [calendar.month_abbr[m].upper() for m in range(13)]  # index 0 unused

# instrumenttype values tried when listing option expiries. Brokers/APIs differ
# on the accepted spelling, so try the documented one first and fall back.
_OPTION_INSTRUMENT_TYPES = ("options", "CE", "OPTIDX")

# Underlying lookup exchange per index. Index options are derivatives of the
# INDEX (quote-only, NSE_INDEX), which is what optionsymbol expects as underlying.
_UNDERLYING_EXCHANGE = {
    "NIFTY": "NSE_INDEX",
    "BANKNIFTY": "NSE_INDEX",
    "FINNIFTY": "NSE_INDEX",
    "MIDCPNIFTY": "NSE_INDEX",
    "NIFTYNXT50": "NSE_INDEX",
    "SENSEX": "BSE_INDEX",
    "BANKEX": "BSE_INDEX",
}


def log(message: str) -> None:
    """Plain-text timestamped logging (no secrets)."""
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# PURE CORE (no network, no SDK -- unit-tested offline)
# ===============================================================================


def build_offsets(strikes: int) -> list[str]:
    """Build the ordered ATM-centred offset ring for ``strikes`` either side.

    Returns deepest-ITM -> ATM -> deepest-OTM so the written watchlist reads in
    strike order for a CE (and mirrored, still money-ordered, for a PE).

    Args:
        strikes: Number of strikes either side of ATM (0 -> ATM only). Negative
            values are treated as 0. Capped at 50, the SDK's ITM/OTM range.

    Returns:
        e.g. ``strikes=2`` -> ``["ITM2", "ITM1", "ATM", "OTM1", "OTM2"]``.
    """
    n = max(0, min(int(strikes), 50))
    itm = [f"ITM{i}" for i in range(n, 0, -1)]
    otm = [f"OTM{i}" for i in range(1, n + 1)]
    return [*itm, "ATM", *otm]


def normalize_option_types(text) -> list[str]:
    """Parse a comma/newline list of option types into a clean ``[CE, PE]`` list.

    Accepts any case and common separators; keeps only the valid ``CE`` / ``PE``
    tokens, de-duplicated in first-seen order. Invalid tokens are dropped so a
    typo can never widen the traded set.

    Args:
        text: Raw text (or None).

    Returns:
        The cleaned list ([] when nothing valid is present).
    """
    if not text:
        return []
    out: list[str] = []
    for chunk in str(text).replace(",", "\n").split("\n"):
        token = chunk.strip().upper()
        if token in ("CE", "PE") and token not in out:
            out.append(token)
    return out


def to_ddmmmyy(expiry_raw) -> str | None:
    """Convert any supported expiry string to the ``DDMMMYY`` the API expects.

    e.g. ``"28-OCT-25"`` / ``"2025-10-28"`` -> ``"28OCT25"``. Returns None when
    the input cannot be parsed (caller skips that expiry rather than guessing).
    """
    parsed = parse_expiry(expiry_raw)
    if parsed is None:
        return None
    year, month, day = parsed
    if not 1 <= month <= 12:
        return None
    return f"{int(day):02d}{_MONTH_ABBR[month]}{int(year) % 100:02d}"


def is_monthly_expiry(expiry_raw, all_expiries) -> bool:
    """Is ``expiry_raw`` the MONTHLY contract (last expiry in its calendar month)?

    Index options list several weeklies per month; the monthly contract is the
    LAST expiry within that month. Decided against the supplied expiry list, so
    it stays correct without a holiday calendar.

    Args:
        expiry_raw: The expiry under test.
        all_expiries: All known expiry strings for the instrument.

    Returns:
        True when no other expiry in the same month falls later.
    """
    parsed = parse_expiry(expiry_raw)
    if parsed is None:
        return False
    year, month, day = parsed
    for other in all_expiries or []:
        other_parsed = parse_expiry(other)
        if other_parsed is None:
            continue
        o_year, o_month, o_day = other_parsed
        if o_year == year and o_month == month and o_day > day:
            return False
    return True


def pick_expiry(expiries, today, kind: str = "weekly", min_days: int = 0) -> str | None:
    """Choose the expiry to trade: nearest (``weekly``) or the month's last.

    ``weekly`` returns the nearest expiry at least ``min_days`` away (for index
    options that is the current weekly, which on expiry week IS the monthly).
    ``monthly`` restricts the candidates to expiries that are the last in their
    calendar month, then takes the nearest qualifying one.

    Args:
        expiries: Raw expiry strings (mixed formats tolerated).
        today: Reference date as ``(year, month, day)``.
        kind: ``"weekly"`` (nearest) or ``"monthly"`` (month-end contract).
        min_days: Skip contracts expiring within this many days (early roll).

    Returns:
        The chosen raw expiry string, or None when nothing qualifies.
    """
    if not expiries:
        return None
    if str(kind).strip().lower() == "monthly":
        candidates = [e for e in expiries if is_monthly_expiry(e, expiries)]
    else:
        candidates = list(expiries)
    return pick_front_month(candidates, today, min_days)


def split_fut_and_opt(symbols) -> tuple[list[str], list[str]]:
    """Split existing watchlist symbols into (futures, options) by suffix.

    Used to tell the operator exactly what a rewrite of the shared NFO.txt would
    replace. Classification is by the symbol's own suffix, so it needs no lookup.

    Returns:
        ``(fut_symbols, option_symbols)``; anything unrecognised is omitted.
    """
    futures: list[str] = []
    options: list[str] = []
    for sym in symbols or []:
        s = str(sym).strip().upper()
        if s.endswith("FUT"):
            futures.append(s)
        elif s.endswith("CE") or s.endswith("PE"):
            options.append(s)
    return futures, options


def merge_symbols(existing, resolved) -> list[str]:
    """Union existing and newly resolved symbols, de-duplicated, existing first.

    Order is stable (existing entries keep their position, new ones append) so a
    merged watchlist does not reshuffle between runs.
    """
    out: list[str] = []
    seen: set[str] = set()
    for group in (existing or [], resolved or []):
        for sym in group:
            s = str(sym).strip().upper()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


# ===============================================================================
# I/O SHELL (SDK discovery, CLI, atomic write) -- lazy SDK/DB imports
# ===============================================================================


def _underlying_exchange(base: str) -> str:
    """Exchange to quote the UNDERLYING index on (defaults to NSE_INDEX)."""
    return _UNDERLYING_EXCHANGE.get((base or "").strip().upper(), "NSE_INDEX")


def _expiry_strings(client, base: str, exchange: str = "NFO") -> list[str] | None:
    """Fetch option expiries for an index base name as raw strings.

    Tries the accepted ``instrumenttype`` spellings in order until one returns a
    usable payload. Any network/shape failure returns None (logged by caller).
    """
    last_error: Exception | None = None
    for instrument_type in _OPTION_INSTRUMENT_TYPES:
        try:
            resp = client.expiry(
                symbol=base, exchange=exchange, instrumenttype=instrument_type
            )
        except Exception as e:  # try the next spelling
            last_error = e
            continue
        data = _response_list(resp)
        if not data:
            continue
        out: list[str] = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                val = item.get("expiry")
                if isinstance(val, str):
                    out.append(val)
        if out:
            return out
    if last_error is not None:
        raise last_error
    return None


def _option_symbol(client, base: str, expiry_ddmmmyy: str, offset: str, option_type: str):
    """Resolve ONE option symbol via the SDK (authoritative). None on failure.

    Returns ``(symbol, lotsize)`` where lotsize may be None if not reported.
    """
    resp = client.optionsymbol(
        underlying=base,
        exchange=_underlying_exchange(base),
        expiry_date=expiry_ddmmmyy,
        offset=offset,
        option_type=option_type,
    )
    if not isinstance(resp, dict):
        return None
    if str(resp.get("status", "success")).lower() == "error":
        return None
    # The payload may be flat or nested under "data".
    body = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    return symbol.strip(), body.get("lotsize")


def resolve_index_options(
    client,
    base: str,
    today: tuple[int, int, int],
    strikes: int,
    option_types: list[str],
    expiry_kind: str,
    min_days: int,
) -> list[str]:
    """Resolve the full strike ring for one index. Never raises.

    Lists expiries, picks the weekly/monthly contract, then resolves every
    (offset, option_type) pair via the SDK. Each pair is guarded independently --
    one unavailable strike never aborts the rest.
    """
    try:
        expiries = _expiry_strings(client, base)
    except Exception as e:
        log(f"{base}: option expiry lookup failed ({type(e).__name__}: {e}) - skipped")
        return []
    if not expiries:
        log(f"{base}: no NFO option expiries returned - skipped")
        return []

    chosen = pick_expiry(expiries, today, expiry_kind, min_days)
    if chosen is None:
        log(
            f"{base}: no {expiry_kind} expiry >= today+{min_days}d among "
            f"{len(expiries)} found - skipped"
        )
        return []

    expiry_ddmmmyy = to_ddmmmyy(chosen)
    if expiry_ddmmmyy is None:
        log(f"{base}: chosen expiry {chosen!r} unparseable - skipped")
        return []

    monthly = is_monthly_expiry(chosen, expiries)
    log(
        f"{base}: {expiry_kind} expiry {expiry_ddmmmyy} "
        f"({'monthly' if monthly else 'weekly'} contract)"
    )

    resolved: list[str] = []
    for offset in build_offsets(strikes):
        for option_type in option_types:
            try:
                got = _option_symbol(client, base, expiry_ddmmmyy, offset, option_type)
            except Exception as e:
                log(f"{base} {offset} {option_type}: lookup failed ({type(e).__name__}: {e})")
                continue
            if not got:
                log(f"{base} {offset} {option_type}: not resolved - skipped")
                continue
            symbol, lotsize = got
            if symbol not in resolved:
                resolved.append(symbol)
                lot = f" lot={lotsize}" if lotsize else ""
                log(f"{base} {offset} {option_type} -> {symbol}{lot}")
    return resolved


def resolve_api_key(user: str | None) -> str | None:
    """Resolve the API key: OPENALGO_API_KEY env, else the local DB for --user.

    The key is returned to the caller and NEVER logged.
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
        f"starter set ({', '.join(DEFAULT_INDICES)})."
    )
    return list(DEFAULT_INDICES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve NFO index option (CE/PE) strikes around ATM to tradable symbols."
    )
    parser.add_argument("--user", help="User id for the DB API-key lookup (if no env key)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--indices", help="Comma-separated index base names (NIFTY,BANKNIFTY,...)")
    group.add_argument("--indices-file", help="File with one index base name per line")
    parser.add_argument(
        "--strikes",
        type=int,
        default=DEFAULT_STRIKES,
        help=f"Strikes either side of ATM (0=ATM only); default {DEFAULT_STRIKES}",
    )
    parser.add_argument(
        "--option-types",
        default="CE,PE",
        help="Option types to resolve: CE, PE, or CE,PE (default CE,PE)",
    )
    parser.add_argument(
        "--expiry-kind",
        choices=("weekly", "monthly"),
        default="weekly",
        help="weekly = nearest expiry; monthly = last expiry of its month",
    )
    parser.add_argument(
        "--min-days-to-expiry",
        type=int,
        default=0,
        help="Skip a contract expiring within this many days (early roll); default 0",
    )
    write_mode = parser.add_mutually_exclusive_group()
    write_mode.add_argument(
        "--merge",
        action="store_true",
        help="Union with the existing NFO.txt (keeps index futures alongside options)",
    )
    write_mode.add_argument(
        "--replace",
        action="store_true",
        help="Replace NFO.txt even when it holds futures (otherwise the run refuses)",
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

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from screener import write_watchlist  # atomic tmp+fsync+replace writer
    from watchlist_loader import parse_symbol_list, watchlist_file_path

    args = build_parser().parse_args(argv)
    min_days = max(0, args.min_days_to_expiry)
    strikes = max(0, args.strikes)

    option_types = normalize_option_types(args.option_types)
    if not option_types:
        log(f"ERROR: no valid option types in {args.option_types!r} (expected CE, PE, or CE,PE)")
        return 1

    indices = load_indices(args)
    if not indices:
        log("ERROR: empty index list - nothing to resolve")
        return 1

    api_key = resolve_api_key(args.user)
    if not api_key:
        log("ERROR: no API key (set OPENALGO_API_KEY or pass --user with a stored key)")
        return 1

    target = watchlist_file_path("NFO", args.watchlist_dir)

    # NFO.txt is shared with the futures resolver. Read it FIRST so we can refuse
    # to silently wipe an existing futures watchlist.
    existing: list[str] = []
    try:
        if target.is_file():
            existing = parse_symbol_list(target.read_text(encoding="utf-8"))
    except OSError as e:
        log(f"WARNING: could not read existing {target} ({e}); treating as empty")
        existing = []
    existing_fut, existing_opt = split_fut_and_opt(existing)
    if existing_fut and not (args.merge or args.replace or args.dry_run):
        log(
            f"ERROR: {target} already holds {len(existing_fut)} FUT symbol(s) "
            f"({', '.join(existing_fut[:3])}{'...' if len(existing_fut) > 3 else ''}). "
            f"Writing options would REPLACE them and stop futures trading. "
            f"Pass --merge to keep both, or --replace to overwrite deliberately."
        )
        return 1

    host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
    ws_url = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")
    from openalgo import api  # lazy import: pure core stays SDK-free

    client = api(api_key=api_key, host=host, ws_url=ws_url)

    now = datetime.now(IST)
    today = (now.year, now.month, now.day)
    offsets = build_offsets(strikes)
    expected = len(indices) * len(offsets) * len(option_types)
    log(
        f"Resolving {len(indices)} index option chains: {', '.join(indices)} | "
        f"{args.expiry_kind} expiry | strikes={strikes} ({', '.join(offsets)}) | "
        f"types={','.join(option_types)} | up to {expected} symbols"
    )

    resolved: list[str] = []
    # Close the SDK client on every path -- FD hygiene, even for a one-shot CLI.
    try:
        for i, base in enumerate(indices):
            resolved.extend(
                s
                for s in resolve_index_options(
                    client, base, today, strikes, option_types, args.expiry_kind, min_days
                )
                if s not in resolved
            )
            if args.sleep > 0 and i < len(indices) - 1:
                time.sleep(args.sleep)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    log(f"Resolved {len(resolved)}/{expected} option symbols")

    final = merge_symbols(existing, resolved) if args.merge else list(resolved)

    # The scanning strategies cap at MAX_SCAN_SYMBOLS (20); anything beyond that
    # is silently dropped at strategy start, so say so here rather than let the
    # operator believe the tail is being traded.
    if len(final) > 20:
        log(
            f"WARNING: {len(final)} symbols exceeds the strategies' MAX_SCAN_SYMBOLS "
            f"(20) - the tail will be dropped at strategy start. Reduce --strikes, "
            f"--indices, or --option-types to fit."
        )

    if args.dry_run:
        log(f"[dry-run] would write {len(final)} symbols to {target} (not writing)")
        for s in final:
            log(f"  {s}")
        return 0

    # Refuse to overwrite a good watchlist with an empty one: an existing NFO.txt
    # is authoritative even when empty, so writing [] after a transient outage
    # would SILENTLY stop all NFO trading.
    if not final and not args.allow_empty:
        log(
            f"Resolved 0 symbols - NOT writing {target} (would disable NFO trading). "
            f"Existing watchlist left untouched. Pass --allow-empty to override."
        )
        return 1

    header = [
        "OpenAlgo NFO index OPTIONS watchlist (auto-resolved)",
        f"generated {now.strftime('%Y-%m-%d %H:%M:%S')} IST",
        f"expiry={args.expiry_kind} strikes={strikes} types={','.join(option_types)} "
        f"min_days_to_expiry={min_days} source=nfo_options_resolver"
        f"{' merged' if args.merge else ''}",
        "one OpenAlgo symbol per line; consumed by strategies via load_watchlist",
    ]
    write_watchlist(target, final, header)
    log(f"Wrote {len(final)} symbols to {target}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
