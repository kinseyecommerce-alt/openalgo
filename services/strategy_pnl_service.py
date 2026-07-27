"""Per-strategy P&L aggregation service.

Computes realized / unrealized / day P&L grouped by the ``strategy`` tag
attached to every order. The netting math is a **pure** function
(``aggregate_strategy_pnl``) with no DB or network access so it can be
unit-tested offline; the IO shell (``build_attributed_fills_live`` /
``build_attributed_fills_sandbox`` / ``get_strategy_pnl``) wires the pure core
to the live broker tradebook/positionbook services or the sandbox tables.

Netting model: average-cost intraday. Per (strategy, symbol) group we split
fills into buys and sells, match the overlapping quantity at
``avg_sell - avg_buy`` for realized P&L, and mark the remaining open leg to the
current LTP for unrealized P&L.
"""

from datetime import date, datetime, timedelta

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# api_types whose request_data carries the strategy tag + symbol/action/qty
_PLACE_API_TYPES = ("placeorder", "placesmartorder")


# ---------------------------------------------------------------------------
# Pure core (NO db, NO network) - unit tested in test/test_strategy_pnl_service.py
# ---------------------------------------------------------------------------


def attribute_fills(orders_by_orderid, fills):
    """Annotate fills with their strategy tag via the orderid -> strategy map.

    Args:
        orders_by_orderid: Mapping of broker ``orderid`` -> ``strategy`` name,
            typically derived from the ``placeorder`` / ``placesmartorder``
            request logs.
        fills: List of dicts, each with ``orderid``, ``symbol``, ``exchange``,
            ``action``, ``quantity``, ``price``.

    Returns:
        A new list of fill dicts, each with a ``strategy`` key set from the map
        (empty string ``""`` when the orderid is not attributed). Fills with a
        non-positive quantity or a price <= 0 are skipped, never fatal.
    """
    attributed = []
    orders_by_orderid = orders_by_orderid or {}
    for fill in fills or []:
        try:
            qty = int(fill.get("quantity", 0) or 0)
            price = float(fill.get("price", 0) or 0)
            if qty <= 0 or price <= 0:
                continue
            orderid = fill.get("orderid")
            strategy = orders_by_orderid.get(orderid, "") if orderid is not None else ""
            attributed.append(
                {
                    "strategy": strategy or "",
                    "symbol": fill.get("symbol", ""),
                    "exchange": fill.get("exchange", ""),
                    "action": str(fill.get("action", "")).upper(),
                    "quantity": qty,
                    "price": price,
                }
            )
        except (TypeError, ValueError):
            # A single malformed fill is skipped, never crashes the batch.
            continue
    return attributed


def _net_group(fills, positions_ltp):
    """Net one (strategy, symbol) group with average-cost math.

    Returns a dict with full-precision ``realized``, ``unrealized``,
    ``open_qty`` (signed: >0 long, <0 short), ``open_side``
    (``LONG`` / ``SHORT`` / ``None``), ``matched`` (closed quantity), and the
    group's ``symbol`` / ``exchange``.
    """
    symbol = fills[0].get("symbol", "") if fills else ""
    exchange = fills[0].get("exchange", "") if fills else ""

    bq = 0  # buy quantity
    bc = 0.0  # buy cost (qty*price)
    sq = 0  # sell quantity
    sv = 0.0  # sell value (qty*price)

    for fill in fills:
        qty = int(fill.get("quantity", 0) or 0)
        price = float(fill.get("price", 0) or 0)
        if qty <= 0 or price <= 0:
            continue
        if str(fill.get("action", "")).upper() == "BUY":
            bq += qty
            bc += qty * price
        else:
            sq += qty
            sv += qty * price

    avg_buy = bc / bq if bq > 0 else 0.0
    avg_sell = sv / sq if sq > 0 else 0.0

    matched = min(bq, sq)
    realized = matched * (avg_sell - avg_buy)

    open_qty = bq - sq
    unrealized = 0.0
    open_side = None
    if open_qty > 0:
        ltp = positions_ltp.get((symbol, exchange), avg_buy)
        unrealized = open_qty * (ltp - avg_buy)
        open_side = "LONG"
    elif open_qty < 0:
        oq = -open_qty
        ltp = positions_ltp.get((symbol, exchange), avg_sell)
        unrealized = oq * (avg_sell - ltp)
        open_side = "SHORT"

    return {
        "symbol": symbol,
        "exchange": exchange,
        "realized": realized,
        "unrealized": unrealized,
        "open_qty": open_qty,
        "open_side": open_side,
        "matched": matched,
    }


def aggregate_strategy_pnl(attributed_fills, positions_ltp):
    """Aggregate per-strategy P&L from attributed fills (pure).

    Args:
        attributed_fills: List of dicts ``{strategy, symbol, exchange, action,
            quantity, price}``. ``action`` is ``BUY`` or ``SELL``.
        positions_ltp: Mapping ``(symbol, exchange)`` -> current LTP (float),
            used to mark open legs to market.

    Returns:
        Dict ``{strategy_name: {realized, unrealized, day_pnl, trades,
        open_symbol, open_qty, open_side}}``. Money values are rounded to 2 dp
        in the output only; full precision is kept internally. Empty input
        returns ``{}``.
    """
    if not attributed_fills:
        return {}

    positions_ltp = positions_ltp or {}

    # Group fills by (strategy, symbol, exchange).
    groups = {}
    for fill in attributed_fills:
        qty = int(fill.get("quantity", 0) or 0)
        price = float(fill.get("price", 0) or 0)
        if qty <= 0 or price <= 0:
            continue
        key = (fill.get("strategy", ""), fill.get("symbol", ""), fill.get("exchange", ""))
        groups.setdefault(key, []).append(fill)

    # Accumulate per strategy.
    per_strategy = {}
    for (strategy, _symbol, _exchange), fills in groups.items():
        netted = _net_group(fills, positions_ltp)
        acc = per_strategy.setdefault(
            strategy,
            {
                "realized": 0.0,
                "unrealized": 0.0,
                "trades": 0,
                "_open_candidates": [],
            },
        )
        acc["realized"] += netted["realized"]
        acc["unrealized"] += netted["unrealized"]
        acc["trades"] += len(fills)
        if netted["open_qty"] != 0:
            acc["_open_candidates"].append(netted)

    # Finalize: pick the single open position (largest abs open_qty) per strategy.
    result = {}
    for strategy, acc in per_strategy.items():
        candidates = acc.pop("_open_candidates")
        open_symbol = None
        open_qty = 0
        open_side = None
        note = None
        if candidates:
            candidates.sort(key=lambda c: abs(c["open_qty"]), reverse=True)
            top = candidates[0]
            open_symbol = top["symbol"]
            open_qty = top["open_qty"]
            open_side = top["open_side"]
            if len(candidates) > 1:
                note = "multi"

        # Round each leg to 2dp and derive day_pnl from the ROUNDED legs, so a
        # displayed row always reconciles: realized + unrealized == day_pnl to
        # the paisa (rounding the full-precision sum independently could show
        # 33.33 + 33.33 = 66.67).
        realized = round(acc["realized"], 2)
        unrealized = round(acc["unrealized"], 2)
        entry = {
            "realized": realized,
            "unrealized": unrealized,
            "day_pnl": round(realized + unrealized, 2),
            "trades": acc["trades"],
            "open_symbol": open_symbol,
            "open_qty": open_qty,
            "open_side": open_side,
        }
        if note:
            entry["note"] = note
        result[strategy] = entry

    return result


def _round_trip_stats(attributed_fills, positions_ltp):
    """Count winning/losing closed round-trips and open positions (pure helper).

    A win/loss is counted per (strategy, symbol) group on its NET realized:
    a group with ``matched > 0`` and ``realized > 0`` is a win, ``realized < 0``
    a loss (exact break-even counts as neither). Note this is a per-symbol-net
    measure -- several intraday round-trips in the same symbol collapse into one
    win or loss -- not a per-order tally. ``open_positions`` counts groups with
    a nonzero open leg.
    """
    positions_ltp = positions_ltp or {}
    groups = {}
    for fill in attributed_fills or []:
        qty = int(fill.get("quantity", 0) or 0)
        price = float(fill.get("price", 0) or 0)
        if qty <= 0 or price <= 0:
            continue
        key = (fill.get("strategy", ""), fill.get("symbol", ""), fill.get("exchange", ""))
        groups.setdefault(key, []).append(fill)

    wins = losses = open_positions = 0
    for fills in groups.values():
        netted = _net_group(fills, positions_ltp)
        if netted["matched"] > 0:
            if netted["realized"] > 0:
                wins += 1
            elif netted["realized"] < 0:
                losses += 1
        if netted["open_qty"] != 0:
            open_positions += 1
    return wins, losses, open_positions


# ---------------------------------------------------------------------------
# IO shell (db access, guarded - never raises to the caller)
# ---------------------------------------------------------------------------


def _day_bounds(day):
    """Return (start, next_day_start) naive datetimes for a ``date``."""
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def build_attributed_fills_live(user_id, day):
    """Build attributed fills for live mode from OrderLog + the tradebook.

    Reads the day's ``placeorder`` / ``placesmartorder`` rows from ``order_logs``
    to map ``orderid`` -> ``strategy``, then joins that against the broker
    tradebook fills. All parsing is guarded; a bad row is skipped, never fatal.

    IMPORTANT - live fills exist only for the CURRENT trading day. The broker
    tradebook API (``get_tradebook``) is not date-parameterised: it always
    returns the *current* day's fills and the broker resets it daily (~3 AM IST).
    OpenAlgo does not persist live executed fill prices historically (the
    ``placeorder`` response carries only the ``orderid``, never the average fill
    price). Joining a past day's ``order_logs`` against today's tradebook would
    therefore fabricate a track record. To make that structurally impossible,
    this function returns ``[]`` for any ``day`` that is not today (IST). Live
    per-day history beyond today is unavailable by design; the analyzer/sandbox
    path (``build_attributed_fills_sandbox``) persists every trade with its price
    and so supports the full historical range.

    Args:
        user_id: OpenAlgo session user id.
        day: ``datetime.date`` (IST) to read orders for. Must be today (IST) to
            yield fills; any earlier/later day yields ``[]``.

    Returns:
        List of attributed fill dicts (possibly empty).
    """
    # The broker tradebook only ever holds the current trading day's fills, so a
    # request for any other day cannot be answered from real data. Refuse rather
    # than join stale OrderLog rows against today's tradebook (which would
    # fabricate history). strategy-pnl only ever asks for today, so this is a
    # no-op there. Checked BEFORE the DB/service imports so a past-day call does
    # no I/O at all.
    if day != datetime.now(IST).date():
        return []

    import json

    from database.apilog_db import OrderLog, db_session
    from database.auth_db import get_api_key_for_tradingview
    from services.tradebook_service import get_tradebook

    orders_by_orderid = {}
    try:
        start, end = _day_bounds(day)
        # created_at is stored as IST-localized datetime; compare against the
        # IST-localized day window so we do not drift across the tz boundary.
        ist_start = IST.localize(start)
        ist_end = IST.localize(end)
        try:
            rows = (
                db_session.query(OrderLog)
                .filter(
                    OrderLog.api_type.in_(_PLACE_API_TYPES),
                    OrderLog.created_at >= ist_start,
                    OrderLog.created_at < ist_end,
                )
                .all()
            )
            for row in rows:
                try:
                    req = json.loads(row.request_data) if row.request_data else {}
                    resp = json.loads(row.response_data) if row.response_data else {}
                    strategy = req.get("strategy", "") or ""
                    orderid = resp.get("orderid")
                    if orderid is not None:
                        orders_by_orderid[str(orderid)] = strategy
                except Exception:
                    # Skip a single malformed log row.
                    continue
        finally:
            db_session.remove()
    except Exception as e:
        logger.exception(f"Error building live order->strategy map: {e}")
        orders_by_orderid = {}

    fills = []
    try:
        api_key = get_api_key_for_tradingview(user_id)
        if api_key:
            success, response, _status = get_tradebook(api_key=api_key)
            if success and isinstance(response, dict):
                for trade in response.get("data", []) or []:
                    fills.append(
                        {
                            "orderid": str(trade.get("orderid"))
                            if trade.get("orderid") is not None
                            else None,
                            "symbol": trade.get("symbol", ""),
                            "exchange": trade.get("exchange", ""),
                            "action": trade.get("action", ""),
                            "quantity": trade.get("quantity", 0),
                            "price": trade.get("average_price", 0),
                        }
                    )
    except Exception as e:
        logger.exception(f"Error fetching live tradebook for P&L: {e}")
        fills = []

    return attribute_fills(orders_by_orderid, fills)


def build_attributed_fills_sandbox(user_id, day):
    """Build attributed fills for sandbox/analyzer mode from SandboxTrades.

    The strategy tag and execution price live directly on each trade row, so no
    orderid join is needed. All access is guarded; on error returns ``[]``.

    Args:
        user_id: OpenAlgo session user id.
        day: ``datetime.date`` to read trades for.

    Returns:
        List of attributed fill dicts (possibly empty).
    """
    from database.sandbox_db import SandboxTrades, db_session

    fills = []
    try:
        start, end = _day_bounds(day)
        try:
            rows = (
                db_session.query(SandboxTrades)
                .filter(
                    SandboxTrades.user_id == user_id,
                    SandboxTrades.trade_timestamp >= start,
                    SandboxTrades.trade_timestamp < end,
                )
                .all()
            )
            raw = []
            for row in rows:
                try:
                    raw.append(
                        {
                            "orderid": row.orderid,
                            "symbol": row.symbol,
                            "exchange": row.exchange,
                            "action": row.action,
                            "quantity": int(row.quantity),
                            "price": float(row.price),
                            "strategy": row.strategy or "",
                        }
                    )
                except Exception:
                    continue
        finally:
            db_session.remove()

        # Sandbox trades already carry the strategy tag; annotate directly.
        for r in raw:
            qty = r["quantity"]
            price = r["price"]
            if qty <= 0 or price <= 0:
                continue
            fills.append(
                {
                    "strategy": r["strategy"],
                    "symbol": r["symbol"],
                    "exchange": r["exchange"],
                    "action": str(r["action"]).upper(),
                    "quantity": qty,
                    "price": price,
                }
            )
    except Exception as e:
        logger.exception(f"Error building sandbox attributed fills: {e}")
        fills = []

    return fills


def _build_positions_ltp(api_key):
    """Return a mapping ``(symbol, exchange) -> ltp`` from the positionbook.

    Uses ``get_positionbook`` which transparently returns sandbox positions in
    analyzer mode and live positions otherwise. Guarded; on error returns ``{}``.
    """
    from services.positionbook_service import get_positionbook

    positions_ltp = {}
    try:
        if not api_key:
            return positions_ltp
        success, response, _status = get_positionbook(api_key=api_key)
        if success and isinstance(response, dict):
            for pos in response.get("data", []) or []:
                try:
                    symbol = pos.get("symbol", "")
                    exchange = pos.get("exchange", "")
                    ltp = float(pos.get("ltp", 0) or 0)
                    if symbol and ltp > 0:
                        positions_ltp[(symbol, exchange)] = ltp
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.exception(f"Error building positions LTP map for P&L: {e}")
        positions_ltp = {}
    return positions_ltp


def get_strategy_pnl(user_id):
    """Compute per-strategy and portfolio P&L for the given user.

    Detects analyzer (sandbox) vs live mode via ``get_analyze_mode`` - the same
    check used by the tradebook/positionbook services - and reads fills from the
    matching source. Never raises; on error returns an error envelope.

    Args:
        user_id: OpenAlgo session user id.

    Returns:
        ``{"status": "success", "data": {"per_strategy": {...}, "totals": {...},
        "mode": "live"|"analyzer"}}`` or ``{"status": "error", "message": ...}``.
    """
    try:
        from database.auth_db import get_api_key_for_tradingview
        from database.settings_db import get_analyze_mode

        analyze = bool(get_analyze_mode())
        mode = "analyzer" if analyze else "live"
        day = datetime.now(IST).date()

        api_key = None
        try:
            api_key = get_api_key_for_tradingview(user_id)
        except Exception:
            api_key = None

        if analyze:
            attributed_fills = build_attributed_fills_sandbox(user_id, day)
        else:
            attributed_fills = build_attributed_fills_live(user_id, day)

        positions_ltp = _build_positions_ltp(api_key)

        all_strategy = aggregate_strategy_pnl(attributed_fills, positions_ltp)
        # Separate account fills that carry no strategy tag (manual orders, or
        # orders placed by other tools). They must NOT be attributed to any
        # strategy, nor counted in the strategy win-rate/totals -- otherwise
        # the "per-strategy" view is polluted by account-wide activity. They
        # are surfaced separately as an untagged bucket for transparency.
        untagged = all_strategy.pop("", None)
        per_strategy = all_strategy

        # win/loss and open-position stats over TAGGED strategies only.
        tagged_fills = [f for f in (attributed_fills or []) if f.get("strategy")]
        wins, losses, open_positions = _round_trip_stats(tagged_fills, positions_ltp)

        # Totals are the sum of the DISPLAYED (rounded) per-strategy rows, so
        # the dashboard's total always equals the visible rows added up.
        total_realized = round(sum(s["realized"] for s in per_strategy.values()), 2)
        total_unrealized = round(sum(s["unrealized"] for s in per_strategy.values()), 2)
        total_day = round(sum(s["day_pnl"] for s in per_strategy.values()), 2)
        total_trades = sum(s["trades"] for s in per_strategy.values())
        decided = wins + losses
        # win_rate here = share of closed (strategy, symbol) round-trips that
        # netted positive. It is a per-symbol-net measure, not per-order.
        win_rate = round((wins / decided) * 100, 2) if decided > 0 else 0.0

        totals = {
            "day_pnl": total_day,
            "realized": total_realized,
            "unrealized": total_unrealized,
            "open_positions": open_positions,
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            # untagged/manual account P&L, excluded from the strategy figures.
            "untagged_day_pnl": round(untagged["day_pnl"], 2) if untagged else 0.0,
        }

        return {
            "status": "success",
            "data": {
                "per_strategy": per_strategy,
                "totals": totals,
                "mode": mode,
            },
        }
    except Exception as e:
        logger.exception(f"Error computing strategy P&L: {e}")
        return {"status": "error", "message": str(e)}
