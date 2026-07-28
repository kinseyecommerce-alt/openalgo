"""Pre-trade capital and position-count guard.

Enforces the daily capital allocation as a HARD limit: once the allocated
capital is committed, or the maximum concurrent position count is reached, no
further exposure-increasing order is accepted.

Checked at ``place_order_with_auth`` — the single choke point both the live and
sandbox/analyzer order paths pass through — so no strategy can bypass it.

THE RULE THAT MATTERS MOST: only exposure-INCREASING orders are ever blocked.
An order that reduces or closes an existing position is always allowed, even
when the account is over every limit. A cap that blocked exits could trap a
losing position and turn a risk control into the risk.

Layers:
- PURE core (``check_order``): no DB, no network. Given the order, the current
  positions and the limits, it decides allow/block. Unit-tested offline.
- IO shell (``enforce_pre_trade``): fetches positions and resolves the limits.

Failure policy (deliberate, and stated rather than silent): if the position data
cannot be fetched, the guard ALLOWS the order and logs an error. Without
positions it is impossible to tell an entry from an exit, so failing closed
would block exits too — the one thing this guard must never do. The daily loss
limit in ``risk_monitor_service`` remains the authoritative backstop.
"""

from utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure core (NO db, NO network) - unit tested in test/test_capital_guard_service.py
# ---------------------------------------------------------------------------


def _position_key(symbol, exchange, product):
    """Normalized identity of a position leg."""
    return (
        str(symbol or "").strip().upper(),
        str(exchange or "").strip().upper(),
        str(product or "").strip().upper(),
    )


def summarize_positions(positions):
    """Reduce a positionbook into the figures the guard needs (pure).

    Args:
        positions: List of dicts with ``symbol``, ``exchange``, ``product``,
            ``quantity`` (signed; >0 long, <0 short) and ``average_price``.
            Broker adapters emit these as strings, so all are coerced.

    Returns:
        ``{"deployed": float, "open_count": int, "by_key": {key: signed_qty}}``
        where ``deployed`` is the capital committed to OPEN legs
        (``Σ |qty| × average_price``) and ``open_count`` counts legs with a
        non-zero quantity. Flat legs (qty 0) are ignored — the broker keeps
        squared-off rows in the book and they hold no capital.
    """
    deployed = 0.0
    open_count = 0
    by_key = {}
    for pos in positions or []:
        try:
            qty = int(float(pos.get("quantity", 0) or 0))
            avg = float(pos.get("average_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        key = _position_key(pos.get("symbol"), pos.get("exchange"), pos.get("product"))
        # Sum duplicates rather than overwrite, so a book that reports a symbol
        # more than once cannot hide exposure from the guard.
        by_key[key] = by_key.get(key, 0) + qty
        if qty != 0:
            open_count += 1
            deployed += abs(qty) * max(avg, 0.0)
    return {"deployed": round(deployed, 2), "open_count": open_count, "by_key": by_key}


def is_increasing(action, current_qty):
    """Does this order increase exposure on a leg currently at ``current_qty``?

    A BUY adds exposure when flat or long, and reduces it when short; a SELL is
    the mirror. Anything that reduces is an exit and is never blocked.
    """
    act = str(action or "").strip().upper()
    if act == "BUY":
        return current_qty >= 0
    if act == "SELL":
        return current_qty <= 0
    # Unknown action: treat as increasing so an unrecognised order can never
    # slip past the limits by looking like an exit.
    return True


def check_order(
    order,
    positions,
    allocated_capital,
    max_positions,
    order_price,
    enabled=True,
):
    """Decide whether an order may proceed under the capital/position limits.

    Args:
        order: Dict with ``symbol``, ``exchange``, ``product``, ``action``,
            ``quantity``.
        positions: The current positionbook (see :func:`summarize_positions`).
        allocated_capital: Rupees allocated for the day. Exposure-increasing
            orders may not push committed capital above this.
        max_positions: Maximum concurrent open positions. ``<= 0`` disables the
            count check (matching the daily-loss-limit convention, where a
            non-positive limit means "not enforced").
        order_price: Price used to value this order (limit price, or LTP for a
            market order). A non-positive price makes the capital check
            unenforceable, so it is skipped and reported.
        enabled: Master switch; when False every order is allowed.

    Returns:
        ``(allowed: bool, reason: str | None, detail: dict)``. ``reason`` is a
        human-readable rejection message, and ``detail`` carries the figures the
        decision used so the caller can log or surface them.
    """
    summary = summarize_positions(positions)
    detail = {
        "deployed": summary["deployed"],
        "open_count": summary["open_count"],
        "allocated": round(float(allocated_capital or 0), 2),
        "max_positions": int(max_positions or 0),
        "order_value": 0.0,
        "is_exit": False,
    }

    if not enabled:
        return True, None, detail

    try:
        qty = abs(int(float(order.get("quantity", 0) or 0)))
    except (TypeError, ValueError):
        qty = 0

    key = _position_key(order.get("symbol"), order.get("exchange"), order.get("product"))
    current_qty = summary["by_key"].get(key, 0)
    increasing = is_increasing(order.get("action"), current_qty)
    detail["is_exit"] = not increasing

    # Exits always pass. This is the guard's most important property: never trap
    # an open position behind a risk limit.
    if not increasing:
        return True, None, detail

    # Position-count limit. Only a genuinely NEW leg consumes a slot; adding to
    # an existing position does not, so scaling in is never blocked by count.
    limit = int(max_positions or 0)
    if limit > 0 and current_qty == 0 and summary["open_count"] >= limit:
        return (
            False,
            (
                f"Max concurrent positions reached ({summary['open_count']}/{limit}). "
                f"New position in {key[0]} rejected; exits are still allowed."
            ),
            detail,
        )

    # Capital limit.
    try:
        price = float(order_price or 0)
    except (TypeError, ValueError):
        price = 0.0

    if price <= 0 or qty <= 0:
        # Cannot value the order, so the capital cap cannot be applied to it.
        # Reported so the caller can log it rather than assume it was enforced.
        detail["capital_check_skipped"] = True
        return True, None, detail

    order_value = qty * price
    detail["order_value"] = round(order_value, 2)

    allocated = float(allocated_capital or 0)
    if summary["deployed"] + order_value > allocated:
        remaining = max(allocated - summary["deployed"], 0.0)
        return (
            False,
            (
                f"Daily capital allocation exceeded. Allocated {allocated:,.2f}, "
                f"already deployed {summary['deployed']:,.2f}, this order needs "
                f"{order_value:,.2f} (remaining {remaining:,.2f}). "
                f"Exits are still allowed."
            ),
            detail,
        )

    return True, None, detail


# ---------------------------------------------------------------------------
# IO shell (broker + config access, guarded - never raises to the caller)
# ---------------------------------------------------------------------------


def _order_price(order_data, api_key):
    """Best available price to value the order: limit price, else live LTP."""
    try:
        pricetype = str(order_data.get("pricetype", "")).strip().upper()
        if pricetype in ("LIMIT", "SL"):
            price = float(order_data.get("price", 0) or 0)
            if price > 0:
                return price
        from services.quotes_service import get_quotes

        success, response, _status = get_quotes(
            symbol=order_data.get("symbol", ""),
            exchange=order_data.get("exchange", ""),
            api_key=api_key,
        )
        if success and isinstance(response, dict):
            data = response.get("data") or {}
            return float(data.get("ltp", 0) or 0)
    except Exception as e:
        logger.exception(f"Capital guard: could not price order: {e}")
    return 0.0


def enforce_pre_trade(order_data, api_key):
    """Run the pre-trade guard for one order.

    Resolves the limits and the live positionbook, then applies
    :func:`check_order`. See the module docstring for the failure policy: on any
    data failure the order is ALLOWED and the problem logged, because an entry
    cannot be told from an exit without positions.

    Args:
        order_data: The validated order dict.
        api_key: OpenAlgo API key for the position/quote lookups.

    Returns:
        ``(allowed: bool, reason: str | None)``.
    """
    try:
        from services.capital_service import resolve_allocation
        from services.risk_monitor_service import load_risk_config

        cfg = load_risk_config()
        if not cfg.get("capital_guard_enabled", False):
            return True, None

        from services.funds_service import get_funds
        from services.positionbook_service import get_positionbook

        available = 0.0
        try:
            ok, funds_resp, _ = get_funds(api_key=api_key)
            if ok and isinstance(funds_resp, dict):
                available = float((funds_resp.get("data") or {}).get("availablecash", 0) or 0)
        except Exception as e:
            logger.exception(f"Capital guard: funds lookup failed: {e}")
            return True, None

        allocation = resolve_allocation(
            available,
            cfg.get("capital_mode"),
            cfg.get("capital_amount"),
            cfg.get("capital_percent"),
        )

        try:
            ok, pos_resp, _ = get_positionbook(api_key=api_key)
        except Exception as e:
            logger.exception(f"Capital guard: positionbook lookup failed: {e}")
            return True, None
        if not ok or not isinstance(pos_resp, dict):
            logger.error(
                "Capital guard: positionbook unavailable; allowing order so exits "
                "are never blocked. The daily loss limit remains the backstop."
            )
            return True, None
        positions = pos_resp.get("data") or []

        price = _order_price(order_data, api_key)

        allowed, reason, detail = check_order(
            order_data,
            positions,
            allocation["allocated"],
            cfg.get("max_positions", 0),
            price,
            enabled=True,
        )
        if not allowed:
            logger.warning(f"Capital guard BLOCKED order: {reason} | {detail}")
        elif detail.get("capital_check_skipped"):
            logger.warning(
                f"Capital guard: could not price {order_data.get('symbol')}; "
                f"capital cap not applied to this order."
            )
        return allowed, reason
    except Exception as e:
        # Never let the guard itself break order placement.
        logger.exception(f"Capital guard failed open: {e}")
        return True, None
