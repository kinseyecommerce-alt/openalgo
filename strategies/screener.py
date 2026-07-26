"""Pre-market watchlist screener for OpenAlgo.

Ranks a universe of OpenAlgo symbols by LIQUIDITY (average traded turnover /
volume) and VOLATILITY (ATR% / ADR% of daily bars), then writes the top-K
symbols to the per-exchange screened watchlist file that the scanning
strategies consume via strategies/watchlist_loader.load_watchlist:

    <watchlist-dir>/<EXCHANGE>.txt   (default: strategies/watchlists/NSE.txt)

The strategies read that file only when their WATCHLIST env var is unset (env
still wins), so the screener is an OPT-IN pre-market step: run it, and the next
strategy start picks up the freshly screened universe.

Layers:
  - PURE ranking core (no network, unit-tested offline):
        compute_metrics(candles)                -> metrics dict | None
        rank_symbols(metrics_by_symbol, ...)    -> list[str]
  - I/O shell (SDK history fetch, CLI, atomic file write). The SDK and the
    database are imported LAZILY so the pure core imports with no dependencies.

Universe input is a list of TRADABLE OpenAlgo SYMBOLS (e.g. RELIANCE, SBIN) --
NOT company names. A NIFTY-500 spreadsheet of company names is NOT a valid
universe file; map names to OpenAlgo symbols first.

Run:
    uv run python strategies/screener.py --user <id> [--exchange NSE] \
        [--universe SYM1,SYM2 | --universe-file path] \
        [--lookback 20] [--top-k 15] [--min-turnover 50000000] \
        [--min-price 50] [--max-price 10000] [--sort-key atr_pct] \
        [--interval D] [--sleep 0.2] [--dry-run] [--watchlist-dir strategies/watchlists]

The API key is read from OPENALGO_API_KEY, else looked up for --user from the
local database (like the /python strategy host does). No secret is ever logged.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE liquid-F&O default universe -- mirrors the scanning strategies' fallback.
# This is a STARTER set only: supply a real universe with --universe-file for
# a meaningful screen.
DEFAULT_NSE_UNIVERSE = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "SBIN",
    "AXISBANK",
    "LT",
    "ITC",
    "TATAMOTORS",
]

VALID_SORT_KEYS = ("atr_pct", "adr_pct", "avg_turnover", "avg_volume")


def log(message: str) -> None:
    """Plain-text timestamped logging (no secrets)."""
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# PURE RANKING CORE (no network, no SDK -- unit-tested offline)
# ===============================================================================


def compute_metrics(candles: list[dict], lookback: int = 20) -> dict | None:
    """Compute liquidity and volatility metrics from daily bars.

    The most recent bar is treated as today's PARTIAL bar and dropped (mirroring
    the intraday strategies' ``df.iloc[:-1]``); metrics are computed over the
    ``lookback`` most recent COMPLETED bars.

    Definitions over the lookback window (last close = window's final close):
        avg_turnover = mean(close * volume)
        avg_volume   = mean(volume)
        atr_pct      = mean(true range) / last_close * 100
        adr_pct      = mean((high - low) / close) * 100
        last_close   = close of the last completed bar

    True range of the first window bar is (high - low); subsequent bars use the
    Wilder true range against the prior bar's close.

    Args:
        candles: Daily bars as dicts with open/high/low/close/volume, oldest
            first. May be shorter than required.
        lookback: Number of completed bars to measure (default 20).

    Returns:
        The metrics dict, or None when there are too few bars or the last close
        is not positive.
    """
    if not candles or lookback <= 0 or len(candles) < lookback + 1:
        return None
    completed = candles[:-1]  # drop today's partial bar
    if len(completed) < lookback:
        return None
    window = completed[-lookback:]

    highs = [float(b["high"]) for b in window]
    lows = [float(b["low"]) for b in window]
    closes = [float(b["close"]) for b in window]
    vols = [float(b.get("volume", 0) or 0) for b in window]

    last_close = closes[-1]
    if last_close <= 0:
        return None

    true_ranges = []
    for i in range(len(window)):
        if i == 0:
            true_ranges.append(highs[i] - lows[i])
        else:
            true_ranges.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    n = len(window)
    atr = sum(true_ranges) / n
    adr = sum((highs[i] - lows[i]) / closes[i] for i in range(n) if closes[i] > 0) / n
    avg_turnover = sum(closes[i] * vols[i] for i in range(n)) / n
    avg_volume = sum(vols) / n

    return {
        "avg_turnover": avg_turnover,
        "avg_volume": avg_volume,
        "atr_pct": atr / last_close * 100.0,
        "adr_pct": adr * 100.0,
        "last_close": last_close,
    }


def rank_symbols(
    metrics_by_symbol: dict[str, dict],
    min_turnover: float,
    min_price: float,
    max_price: float,
    top_k: int,
    sort_key: str = "atr_pct",
) -> list[str]:
    """Filter and rank symbols by their metrics.

    Drops symbols whose metrics are None, whose avg_turnover is below
    ``min_turnover``, or whose last_close is outside ``[min_price, max_price]``.
    The survivors are sorted by ``sort_key`` descending, tie-broken by
    avg_turnover descending, then symbol ascending (fully deterministic). The
    top ``top_k`` symbols are returned.

    Args:
        metrics_by_symbol: Mapping of symbol -> metrics dict (or None).
        min_turnover: Minimum average turnover (liquidity floor).
        min_price: Inclusive lower bound on last_close.
        max_price: Inclusive upper bound on last_close.
        top_k: Number of symbols to return.
        sort_key: One of atr_pct / adr_pct / avg_turnover / avg_volume.

    Returns:
        The ranked list of up to ``top_k`` symbols.

    Raises:
        ValueError: If ``sort_key`` is not a valid metric key.
    """
    if sort_key not in VALID_SORT_KEYS:
        raise ValueError(
            f"invalid sort_key {sort_key!r}; choose one of {', '.join(VALID_SORT_KEYS)}"
        )
    eligible: list[tuple[str, dict]] = []
    for symbol, metrics in metrics_by_symbol.items():
        if not metrics:
            continue
        if metrics["avg_turnover"] < min_turnover:
            continue
        if not (min_price <= metrics["last_close"] <= max_price):
            continue
        eligible.append((symbol, metrics))

    eligible.sort(
        key=lambda item: (-item[1][sort_key], -item[1]["avg_turnover"], item[0])
    )
    if top_k < 0:
        top_k = 0
    return [symbol for symbol, _ in eligible[:top_k]]


# ===============================================================================
# I/O SHELL (SDK history fetch, CLI, atomic write) -- lazy SDK/DB imports
# ===============================================================================


def _df_to_candles(df) -> list[dict]:
    """Convert an openalgo history DataFrame into the pure layer's dicts."""
    if df is None or getattr(df, "empty", True):
        return []
    columns = getattr(df, "columns", [])
    n = len(df)
    vols = df["volume"].tolist() if "volume" in columns else [0.0] * n
    return [
        {
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
            "volume": float(v or 0),
        }
        for o, h, lo, c, v in zip(
            df["open"].tolist(),
            df["high"].tolist(),
            df["low"].tolist(),
            df["close"].tolist(),
            vols,
            strict=True,
        )
    ]


def resolve_api_key(user: str | None) -> str | None:
    """Resolve the API key: OPENALGO_API_KEY env, else the local DB for --user.

    The database import is wrapped so the pure/offline path never needs Flask
    or the SDK. The key is returned to the caller and NEVER logged.
    """
    key = os.getenv("OPENALGO_API_KEY")
    if key:
        return key
    if not user:
        return None
    try:
        # Ensure the repo root is importable when run as a script.
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from database.auth_db import get_api_key_for_tradingview

        return get_api_key_for_tradingview(user)
    except Exception as e:
        log(f"API key lookup for user failed ({type(e).__name__}); set OPENALGO_API_KEY: {e}")
        return None


def load_universe(args) -> list[str]:
    """Resolve the universe (list of tradable OpenAlgo SYMBOLS) from CLI args."""
    # Local import keeps the pure core free of the loader (still pure, but the
    # parsing lives in one place).
    from watchlist_loader import parse_symbol_list

    if args.universe:
        return parse_symbol_list(args.universe)
    if args.universe_file:
        text = Path(args.universe_file).read_text(encoding="utf-8")
        return parse_symbol_list(text)
    log(
        f"No --universe/--universe-file given; using the {len(DEFAULT_NSE_UNIVERSE)}-symbol "
        "NSE starter list. Supply a real symbol universe with --universe-file for a "
        "meaningful screen (SYMBOLS, not company names)."
    )
    return list(DEFAULT_NSE_UNIVERSE)


def fetch_metrics(client, symbol, exchange, interval, lookback):
    """Fetch history for one symbol and compute its metrics, or None on any
    failure (logged as a warning -- a single bad symbol never crashes the run)."""
    end = datetime.now(IST)
    # ~2x lookback calendar days covers weekends/holidays for `lookback` bars.
    start = end - timedelta(days=max(5, lookback * 2 + 5))
    try:
        df = client.history(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log(f"{symbol}: history fetch failed ({type(e).__name__}: {e}) - skipped")
        return None
    candles = _df_to_candles(df)
    if not candles:
        log(f"{symbol}: no history returned - skipped")
        return None
    metrics = compute_metrics(candles, lookback)
    if metrics is None:
        log(f"{symbol}: too few bars for lookback {lookback} - skipped")
    return metrics


def write_watchlist(path: Path, symbols: list[str], header_lines: list[str]) -> None:
    """Atomically write the screened watchlist (tmp + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for line in header_lines:
            f.write(f"# {line}\n")
        for symbol in symbols:
            f.write(f"{symbol}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-market liquidity + volatility watchlist screener for OpenAlgo."
    )
    parser.add_argument("--user", help="User id for the DB API-key lookup (if no env key)")
    parser.add_argument("--exchange", default="NSE", help="OpenAlgo exchange (default NSE)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--universe", help="Comma-separated OpenAlgo symbols")
    group.add_argument("--universe-file", help="File with one OpenAlgo symbol per line")
    parser.add_argument("--lookback", type=int, default=20, help="Completed bars to measure")
    parser.add_argument("--top-k", type=int, default=15, help="Symbols to keep")
    parser.add_argument("--min-turnover", type=float, default=50_000_000.0, help="Liquidity floor")
    parser.add_argument("--min-price", type=float, default=50.0, help="Min last close")
    parser.add_argument("--max-price", type=float, default=10_000.0, help="Max last close")
    parser.add_argument(
        "--sort-key", default="atr_pct", choices=VALID_SORT_KEYS, help="Ranking metric"
    )
    parser.add_argument("--interval", default="D", help="History interval (default D)")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds between API calls")
    parser.add_argument("--dry-run", action="store_true", help="Print result, do not write")
    parser.add_argument("--watchlist-dir", help="Output dir (default strategies/watchlists)")
    return parser


def main(argv: list[str] | None = None) -> int:
    import time

    # Make the sibling watchlist_loader importable however main() is invoked.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from watchlist_loader import watchlist_file_path

    args = build_parser().parse_args(argv)
    exchange = args.exchange.strip().upper()
    if args.lookback <= 0:
        log("ERROR: --lookback must be positive")
        return 1

    universe = load_universe(args)
    if not universe:
        log("ERROR: empty universe - nothing to screen")
        return 1

    api_key = resolve_api_key(args.user)
    if not api_key:
        log("ERROR: no API key (set OPENALGO_API_KEY or pass --user with a stored key)")
        return 1

    host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
    ws_url = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")
    from openalgo import api  # lazy import: pure core stays SDK-free

    client = api(api_key=api_key, host=host, ws_url=ws_url)

    log(
        f"Screening {len(universe)} {exchange} symbols on interval {args.interval}, "
        f"lookback {args.lookback}, sort by {args.sort_key} desc, "
        f"min_turnover {args.min_turnover:,.0f}, price [{args.min_price}, {args.max_price}], "
        f"top-k {args.top_k}"
    )

    metrics_by_symbol: dict[str, dict] = {}
    for i, symbol in enumerate(universe):
        metrics_by_symbol[symbol] = fetch_metrics(
            client, symbol, exchange, args.interval, args.lookback
        )
        if args.sleep > 0 and i < len(universe) - 1:
            time.sleep(args.sleep)

    ranked = rank_symbols(
        metrics_by_symbol,
        args.min_turnover,
        args.min_price,
        args.max_price,
        args.top_k,
        args.sort_key,
    )

    scored = sum(1 for m in metrics_by_symbol.values() if m)
    log(f"Ranked {len(ranked)} symbols (from {scored}/{len(universe)} with usable metrics)")
    for rank, symbol in enumerate(ranked, 1):
        m = metrics_by_symbol[symbol]
        log(
            f"  {rank:>2}. {symbol:<16} {args.sort_key}={m[args.sort_key]:.3f} "
            f"turnover={m['avg_turnover']:,.0f} last={m['last_close']:.2f}"
        )

    target = watchlist_file_path(exchange, args.watchlist_dir)
    if args.dry_run:
        log(f"[dry-run] would write {len(ranked)} symbols to {target} (not writing)")
        return 0

    header = [
        f"OpenAlgo screened watchlist - {exchange}",
        f"generated {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST",
        f"sort_key={args.sort_key} lookback={args.lookback} top_k={args.top_k} "
        f"min_turnover={args.min_turnover:.0f} price=[{args.min_price},{args.max_price}] "
        f"interval={args.interval}",
        "one OpenAlgo symbol per line; consumed by strategies via load_watchlist",
    ]
    write_watchlist(target, ranked, header)
    log(f"Wrote {len(ranked)} symbols to {target}")
    return 0


if __name__ == "__main__":
    # Make the sibling watchlist_loader importable when run as a script.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
