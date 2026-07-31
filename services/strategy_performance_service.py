"""Strategy performance (historical track-record) service.

Aggregates the **actual executed trades** of every strategy over a date range
into a per-strategy and portfolio track record: total P&L, win rate, profit
factor, best/worst day, max drawdown, and a daily cumulative curve.

The metrics math is a **pure** function (``summarize_performance``) with no DB
or network access so it can be unit-tested offline; the IO shell
(``get_strategy_performance`` / ``resolve_range``) walks each IST calendar day
in the range, reuses the tested per-day netting machinery in
``services.strategy_pnl_service`` to attribute and net fills, and feeds the
resulting daily series + closed round-trips into the pure core.

Reuse + netting:
- ``build_attributed_fills_live`` / ``build_attributed_fills_sandbox`` produce
  each day's attributed fills (now carrying ``product``).
- ``net_fills_chronological`` nets those fills across the whole range with a
  cross-day, per-product running average-cost inventory, so a position opened
  one day and closed another is counted (per-day netting would miss it) and a
  CNC long never nets against an MIS short on the same symbol.
"""

from datetime import date, datetime, timedelta

import pytz

from services.strategy_pnl_service import (
    build_attributed_fills_live,
    build_attributed_fills_sandbox,
)
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Hard cap on the day loop so a caller-supplied range can never blow up the
# per-day DB/broker fan-out.
MAX_RANGE_DAYS = 180


# ---------------------------------------------------------------------------
# Pure core (NO db, NO network) - unit tested in
# test/test_strategy_performance_service.py
# ---------------------------------------------------------------------------


def _zeroed_summary():
    """The summary for a strategy/portfolio with no trading activity."""
    return {
        "total_pnl": 0.0,
        "trading_days": 0,
        "trade_count": 0,
        "win_trades": 0,
        "loss_trades": 0,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": None,
        "win_days": 0,
        "loss_days": 0,
        "best_day": None,
        "worst_day": None,
        "max_drawdown": 0.0,
        "cumulative": 0.0,
        "daily": [],
    }


def summarize_performance(daily_pnl, trade_results):
    """Summarize a strategy's historical performance (pure).

    Args:
        daily_pnl: List of ``{"date": "YYYY-MM-DD", "pnl": float, "trades":
            int}`` in ascending date order (one entry per trading day the
            strategy was active).
        trade_results: List of float realized P&L, one per CLOSED round-trip (a
            closed ``(strategy, symbol)`` group on a day).

    Returns:
        A dict of track-record metrics. Money values are rounded to 2 dp in the
        output only; full precision is used for every internal calculation.
        Deterministic: identical inputs always yield identical output. Empty
        ``daily_pnl`` returns a fully zeroed summary.
    """
    daily_pnl = daily_pnl or []
    trade_results = trade_results or []

    if not daily_pnl:
        return _zeroed_summary()

    # Daily curve: running cumulative + drawdown off the running peak.
    daily_out = []
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    win_days = 0
    loss_days = 0
    best = None
    worst = None
    total = 0.0

    for entry in daily_pnl:
        day_pnl = float(entry.get("pnl", 0) or 0)
        day_trades = int(entry.get("trades", 0) or 0)
        day_date = entry.get("date")

        total += day_pnl
        cumulative += day_pnl
        if cumulative > peak:
            peak = cumulative
        drawdown = cumulative - peak
        if drawdown < max_drawdown:
            max_drawdown = drawdown

        if day_pnl > 0:
            win_days += 1
        elif day_pnl < 0:
            loss_days += 1

        if best is None or day_pnl > best["pnl"]:
            best = {"date": day_date, "pnl": day_pnl}
        if worst is None or day_pnl < worst["pnl"]:
            worst = {"date": day_date, "pnl": day_pnl}

        daily_out.append(
            {
                "date": day_date,
                "pnl": round(day_pnl, 2),
                "cumulative": round(cumulative, 2),
                "trades": day_trades,
            }
        )

    # Per-trade win/loss stats over the closed round-trips. Break-even trades
    # (exactly 0.0) are excluded from BOTH the win-rate denominator and the
    # profit-factor sums.
    wins = [r for r in trade_results if r > 0]
    losses = [r for r in trade_results if r < 0]
    win_trades = len(wins)
    loss_trades = len(losses)
    decided = win_trades + loss_trades

    win_rate = round((win_trades / decided) * 100, 2) if decided > 0 else 0.0
    avg_win = round(sum(wins) / win_trades, 2) if win_trades > 0 else 0.0
    avg_loss = round(sum(losses) / loss_trades, 2) if loss_trades > 0 else 0.0

    gross_loss = abs(sum(losses))
    profit_factor = round(sum(wins) / gross_loss, 2) if loss_trades > 0 else None

    return {
        "total_pnl": round(total, 2),
        "trading_days": len(daily_pnl),
        "trade_count": len(trade_results),
        "win_trades": win_trades,
        "loss_trades": loss_trades,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "win_days": win_days,
        "loss_days": loss_days,
        "best_day": {"date": best["date"], "pnl": round(best["pnl"], 2)} if best else None,
        "worst_day": {"date": worst["date"], "pnl": round(worst["pnl"], 2)} if worst else None,
        "max_drawdown": round(max_drawdown, 2),
        "cumulative": round(total, 2),
        "daily": daily_out,
    }


def net_fills_chronological(dated_fills):
    """Net attributed fills across a date range with cross-day, per-product
    running inventory (pure).

    Processes fills in ascending day order (stable within a day), maintaining a
    signed average-cost position per ``(strategy, symbol, exchange, product)``.
    Each reducing/closing fill realizes P&L attributed to *that fill's day*;
    when a position returns to flat (or flips through zero) one round-trip
    result -- its accumulated realized since the position was opened -- is
    emitted, attributed to the closing day.

    Why chronological (not per-day) netting:
    - Cross-day carry: an Analyzer CNC/NRML position opened one day and closed
      another is counted correctly. Per-day netting would see only buys on the
      open day and only sells on the close day, report ``matched == 0`` on both,
      and silently omit the completed trade.
    - Per-product isolation: product is part of the position key, so a CNC long
      and an MIS short on the same symbol are two independent positions, never a
      phantom round-trip.

    Boundaries (kept honest, not hidden):
    - Positions still open at the end of the range contribute no realized P&L
      here -- this is a realized track record; unrealized is not marked.
    - A closing fill for a position opened BEFORE the first in-range fill has no
      in-range cost basis; it opens a fresh position at its own price rather than
      matching a phantom. Full history for long carries needs fills from before
      the queried window.

    Args:
        dated_fills: list of dicts ``{strategy, symbol, exchange, product,
            action (BUY/SELL), quantity, price, day ("YYYY-MM-DD")}``.

    Returns:
        ``(strat_daily, strat_trades, untagged_daily)`` where
        - ``strat_daily``: ``{strategy: {day: {"pnl": float, "trades": int}}}``
          -- ``pnl`` is realized attributed to that day (full precision),
          ``trades`` is the number of fills that day.
        - ``strat_trades``: ``{strategy: [round_trip_realized, ...]}`` for TAGGED
          strategies only (the closed round-trips feeding win/loss stats).
        - ``untagged_daily``: ``{day: float}`` realized for the ``""`` strategy.
    """
    ordered = sorted((dated_fills or []), key=lambda f: f.get("day", ""))

    inv = {}  # (strategy, symbol, exchange, product) -> {qty, avg, realized}
    strat_daily = {}
    strat_trades = {}
    untagged_daily = {}

    def _daily(strategy, day):
        return strat_daily.setdefault(strategy, {}).setdefault(
            day, {"pnl": 0.0, "trades": 0}
        )

    for f in ordered:
        try:
            qty = int(f.get("quantity", 0) or 0)
            price = float(f.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue

        strategy = f.get("strategy", "") or ""
        day = f.get("day", "")
        action = str(f.get("action", "")).upper()
        key = (strategy, f.get("symbol", ""), f.get("exchange", ""), f.get("product", ""))

        # Every valid fill counts on its day, open or close.
        _daily(strategy, day)["trades"] += 1

        pos = inv.setdefault(key, {"qty": 0, "avg": 0.0, "realized": 0.0})
        delta = qty if action == "BUY" else -qty
        realized = 0.0

        if pos["qty"] == 0:
            pos["qty"] = delta
            pos["avg"] = price
            pos["realized"] = 0.0
        elif (pos["qty"] > 0) == (delta > 0):
            # Extend the same side -> roll the average cost.
            total_abs = abs(pos["qty"]) + qty
            pos["avg"] = (pos["avg"] * abs(pos["qty"]) + price * qty) / total_abs
            pos["qty"] += delta
        else:
            # Reduce / close / flip against the open side.
            was_long = pos["qty"] > 0
            close_qty = min(qty, abs(pos["qty"]))
            realized = (
                close_qty * (price - pos["avg"])
                if was_long
                else close_qty * (pos["avg"] - price)
            )
            pos["realized"] += realized
            pos["qty"] += delta

            if pos["qty"] == 0:
                # Round-trip complete.
                if strategy:
                    strat_trades.setdefault(strategy, []).append(pos["realized"])
                pos["avg"] = 0.0
                pos["realized"] = 0.0
            elif (pos["qty"] > 0) == was_long:
                # Partial close, still on the original side: keep avg + accum.
                pass
            else:
                # Flipped through zero: old round-trip done, new position opened
                # with the leftover quantity at this fill's price.
                if strategy:
                    strat_trades.setdefault(strategy, []).append(pos["realized"])
                pos["avg"] = price
                pos["realized"] = 0.0

        if realized:
            _daily(strategy, day)["pnl"] += realized
            if not strategy:
                untagged_daily[day] = untagged_daily.get(day, 0.0) + realized

    return strat_daily, strat_trades, untagged_daily


def resolve_range(days=None, start=None, end=None):
    """Resolve a validated ``(start_date, end_date)`` window (pure).

    Precedence: an explicit ``start``/``end`` pair wins; otherwise the last
    ``days`` calendar days ending today IST (default 30). The span is clamped
    to ``MAX_RANGE_DAYS`` (inclusive of both endpoints) by pulling ``start``
    forward. ``start`` may not be after ``end``.

    Args:
        days: Optional trailing window size in calendar days (>= 1).
        start: Optional ``datetime.date`` or ``"YYYY-MM-DD"`` string.
        end: Optional ``datetime.date`` or ``"YYYY-MM-DD"`` string.

    Returns:
        ``(start_date, end_date)`` as ``datetime.date`` objects.

    Raises:
        ValueError: If ``start`` > ``end``, or a date/``days`` value is invalid.
    """
    today = datetime.now(IST).date()

    def _to_date(value):
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()

    start_date = _to_date(start)
    end_date = _to_date(end)

    if start_date is not None or end_date is not None:
        if end_date is None:
            end_date = today
        if start_date is None:
            span = int(days) if days else 30
            if span < 1:
                span = 1
            start_date = end_date - timedelta(days=span - 1)
    else:
        span = int(days) if days else 30
        if span < 1:
            span = 1
        end_date = today
        start_date = end_date - timedelta(days=span - 1)

    if start_date > end_date:
        raise ValueError("start date must not be after end date")

    # Clamp the span to the hard cap by pulling start forward.
    if (end_date - start_date).days > MAX_RANGE_DAYS - 1:
        start_date = end_date - timedelta(days=MAX_RANGE_DAYS - 1)

    return start_date, end_date


# ---------------------------------------------------------------------------
# IO shell (db access, guarded - never raises to the caller)
# ---------------------------------------------------------------------------


def _iter_days(start_date, end_date):
    """Yield every ``date`` in ``[start_date, end_date]`` inclusive."""
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def get_strategy_performance(user_id, start_date, end_date):
    """Compute per-strategy and portfolio performance over a date range.

    Gathers every IST calendar day's attributed fills in
    ``[start_date, end_date]`` (inclusive) from the live or sandbox source per
    ``get_analyze_mode``, tags each fill with its day, and nets the whole range
    with ``net_fills_chronological`` -- a cross-day, per-product running
    average-cost inventory. Realized P&L is attributed to the day the closing
    fill occurs, and each closed round-trip feeds the win/loss stats. This
    counts positions carried across day boundaries and never nets two different
    products on the same symbol together.

    The untagged ``""`` strategy is excluded from ``per_strategy`` and surfaced
    separately as ``untagged_pnl``. Never raises; on error returns an error
    envelope.

    Live mode caveat: ``build_attributed_fills_live`` only returns fills for the
    current trading day (the broker tradebook is not historical), so a live range
    wider than today reflects at most today. The response sets
    ``data.live_partial = True`` in that case so the UI can say so honestly;
    analyzer/sandbox mode has the full history and never sets it.

    Args:
        user_id: OpenAlgo session user id.
        start_date: ``datetime.date`` (IST) inclusive range start.
        end_date: ``datetime.date`` (IST) inclusive range end.

    Returns:
        ``{"status": "success", "data": {...}}`` or
        ``{"status": "error", "message": ...}``.
    """
    try:
        from database.settings_db import get_analyze_mode

        analyze = bool(get_analyze_mode())
        mode = "analyzer" if analyze else "live"

        # Guard the range: clamp to the hard cap, reject an inverted window.
        if start_date > end_date:
            return {"status": "error", "message": "start date must not be after end date"}
        if (end_date - start_date).days > MAX_RANGE_DAYS - 1:
            start_date = end_date - timedelta(days=MAX_RANGE_DAYS - 1)

        # Live mode has fills only for the current trading day (the broker
        # tradebook resets daily and OpenAlgo does not persist live fill prices
        # historically - see build_attributed_fills_live). So a live range wider
        # than today reflects, at most, today. Flag that honestly for the UI
        # rather than presenting a partial window as a full track record.
        today = datetime.now(IST).date()
        live_partial = (not analyze) and (start_date < today <= end_date or end_date < today)

        builder = build_attributed_fills_sandbox if analyze else build_attributed_fills_live

        # Gather every day's attributed fills, tagging each with its day, then
        # net the whole range chronologically (cross-day carry, per-product).
        dated_fills = []
        days_with_fills = set()
        for day in _iter_days(start_date, end_date):
            attributed_fills = builder(user_id, day)
            if not attributed_fills:
                # Not a trading day for these strategies.
                continue
            day_str = day.strftime("%Y-%m-%d")
            days_with_fills.add(day_str)
            for fill in attributed_fills:
                nf = dict(fill)
                nf["day"] = day_str
                dated_fills.append(nf)

        strat_daily, strat_trades, untagged_daily = net_fills_chronological(dated_fills)
        trading_days = len(days_with_fills)
        untagged_pnl = round(sum(untagged_daily.values()), 2)

        # Build per-strategy summaries (exclude the untagged "" bucket) and, in
        # the same pass, accumulate the portfolio daily series over tagged
        # strategies only.
        per_strategy = {}
        portfolio_daily = {}  # date_str -> summed tagged realized that day
        portfolio_trades = []
        for strategy, day_map in strat_daily.items():
            if not strategy:
                continue
            daily_series = [
                {"date": d, "pnl": day_map[d]["pnl"], "trades": day_map[d]["trades"]}
                for d in sorted(day_map.keys())
            ]
            per_strategy[strategy] = summarize_performance(
                daily_series, strat_trades.get(strategy, [])
            )
            for d, v in day_map.items():
                portfolio_daily[d] = portfolio_daily.get(d, 0.0) + v["pnl"]
            portfolio_trades.extend(strat_trades.get(strategy, []))

        # Portfolio totals: one entry per day summed across tagged strategies.
        portfolio_series = [
            {
                "date": d,
                "pnl": portfolio_daily[d],
                "trades": sum(
                    strat_daily[s][d]["trades"]
                    for s in strat_daily
                    if s and d in strat_daily[s]
                ),
            }
            for d in sorted(portfolio_daily.keys())
        ]
        totals = summarize_performance(portfolio_series, portfolio_trades)

        return {
            "status": "success",
            "data": {
                "range": {
                    "start": start_date.strftime("%Y-%m-%d"),
                    "end": end_date.strftime("%Y-%m-%d"),
                    "trading_days": trading_days,
                },
                "mode": mode,
                "live_partial": live_partial,
                "per_strategy": per_strategy,
                "totals": totals,
                "untagged_pnl": untagged_pnl,
            },
        }
    except Exception as e:
        logger.exception(f"Error computing strategy performance: {e}")
        return {"status": "error", "message": str(e)}
