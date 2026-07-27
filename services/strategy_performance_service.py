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

Reuse (no netting re-implemented here):
- ``build_attributed_fills_live`` / ``build_attributed_fills_sandbox`` produce
  the day's attributed fills.
- ``_net_group(fills, {})`` nets one (strategy, symbol) group for a COMPLETED
  past day: with ``positions_ltp={}`` there is no open leg to mark, so for an
  intraday (MIS) day that squares off, ``realized`` is that group's whole-day
  P&L and ``matched > 0`` flags a closed round-trip.
"""

from datetime import date, datetime, timedelta

import pytz

from services.strategy_pnl_service import (
    _net_group,
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

    Walks each IST calendar day in ``[start_date, end_date]`` (inclusive),
    building that day's attributed fills from the live or sandbox source per
    ``get_analyze_mode``. For a completed past day the netting squares off, so
    each ``(strategy, symbol)`` group's ``realized`` (netted with
    ``positions_ltp={}``) is that day's P&L for the group and a closed
    round-trip when ``matched > 0``.

    The untagged ``""`` strategy is excluded from ``per_strategy`` and surfaced
    separately as ``untagged_pnl``. Never raises; on error returns an error
    envelope.

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

        builder = build_attributed_fills_sandbox if analyze else build_attributed_fills_live

        # Per-strategy accumulators over the whole range.
        # strat_daily[name] = {date_str: {"pnl": float, "trades": int}}
        strat_daily = {}
        # strat_trades[name] = list[float]  (closed round-trip realized P&L)
        strat_trades = {}
        # portfolio_daily[date_str] = float  (sum of tagged strategy P&L that day)
        portfolio_daily = {}
        portfolio_trades = []
        untagged_pnl = 0.0
        trading_days = 0

        for day in _iter_days(start_date, end_date):
            attributed_fills = builder(user_id, day)
            if not attributed_fills:
                # Not a trading day for these strategies.
                continue

            day_str = day.strftime("%Y-%m-%d")

            # Group the day's fills by (strategy, symbol, exchange), then net
            # each group for the completed past day (no open leg to mark).
            groups = {}
            for fill in attributed_fills:
                qty = int(fill.get("quantity", 0) or 0)
                price = float(fill.get("price", 0) or 0)
                if qty <= 0 or price <= 0:
                    continue
                key = (
                    fill.get("strategy", ""),
                    fill.get("symbol", ""),
                    fill.get("exchange", ""),
                )
                groups.setdefault(key, []).append(fill)

            # Per-strategy realized total for this day + fill counts.
            day_realized = {}  # strategy -> realized sum
            day_fill_count = {}  # strategy -> number of fills
            for f in attributed_fills:
                strat = f.get("strategy", "")
                day_fill_count[strat] = day_fill_count.get(strat, 0) + 1

            for (strategy, _symbol, _exchange), fills in groups.items():
                netted = _net_group(fills, {})
                day_realized[strategy] = day_realized.get(strategy, 0.0) + netted["realized"]
                # A closed round-trip contributes a per-trade result.
                if netted["matched"] > 0:
                    if strategy:
                        strat_trades.setdefault(strategy, []).append(netted["realized"])
                        portfolio_trades.append(netted["realized"])

            trading_days += 1

            for strategy, realized in day_realized.items():
                if not strategy:
                    # Untagged/manual account activity - surfaced separately.
                    untagged_pnl += realized
                    continue
                strat_daily.setdefault(strategy, {})[day_str] = {
                    "pnl": realized,
                    "trades": day_fill_count.get(strategy, 0),
                }
                portfolio_daily[day_str] = portfolio_daily.get(day_str, 0.0) + realized

        # Build per-strategy summaries.
        per_strategy = {}
        for strategy, day_map in strat_daily.items():
            daily_series = [
                {"date": d, "pnl": day_map[d]["pnl"], "trades": day_map[d]["trades"]}
                for d in sorted(day_map.keys())
            ]
            per_strategy[strategy] = summarize_performance(
                daily_series, strat_trades.get(strategy, [])
            )

        # Portfolio totals: one entry per day summed across strategies.
        portfolio_series = [
            {
                "date": d,
                "pnl": portfolio_daily[d],
                "trades": sum(
                    strat_daily[s][d]["trades"]
                    for s in strat_daily
                    if d in strat_daily[s]
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
                "per_strategy": per_strategy,
                "totals": totals,
                "untagged_pnl": round(untagged_pnl, 2),
            },
        }
    except Exception as e:
        logger.exception(f"Error computing strategy performance: {e}")
        return {"status": "error", "message": str(e)}
