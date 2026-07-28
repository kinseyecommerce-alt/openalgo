"""Daily capital allocation service.

Sources the account's REAL capital from the broker (never a typed-in number)
and resolves how much of it is allocated for the day's trading.

Two layers, matching the codebase convention:
- PURE core (``resolve_allocation``): no DB, no network, no clock. Given the
  broker's available cash and the saved allocation config, it computes the
  effective rupee allocation. Unit-tested offline in
  test/test_capital_service.py.
- IO shell (``get_capital_status``): reads live funds via
  ``services.funds_service.get_funds`` and merges them with the allocation
  persisted in the shared risk config. Never raises; returns an error envelope.

The allocation is persisted alongside the circuit-breaker settings in
``strategies/risk_config.json`` (via ``services.risk_monitor_service``) because
both are daily trading-risk configuration and that module already provides the
atomic write + reentrant lock.

Modes:
- ``percent``: allocate a percentage of the broker's available cash. This is the
  default because it tracks the account automatically as its balance changes.
- ``amount``: allocate a fixed rupee figure. It is CLAMPED to the broker's
  available cash -- you can never allocate capital the account does not have.
"""

from utils.logging import get_logger

logger = get_logger(__name__)

VALID_MODES = ("percent", "amount")


# ---------------------------------------------------------------------------
# Pure core (NO db, NO network) - unit tested in test/test_capital_service.py
# ---------------------------------------------------------------------------


def resolve_allocation(available_cash, mode, amount, percent):
    """Resolve the effective rupee capital allocation (pure).

    Args:
        available_cash: The broker's available cash/margin (float-able). Values
            that are negative or unparseable are treated as 0.0 -- an account
            with no usable margin allocates nothing.
        mode: ``"percent"`` or ``"amount"``. Anything else falls back to
            ``"percent"`` so a corrupt config can never allocate more than
            intended.
        amount: Fixed rupee allocation, used when ``mode == "amount"``.
        percent: Percentage of available cash, used when ``mode == "percent"``.
            Clamped to 0-100.

    Returns:
        A dict::

            {"available_cash": float,   # what the broker reports (>= 0)
             "mode": "percent"|"amount",
             "percent": float,          # clamped 0-100
             "amount": float,           # requested fixed amount (>= 0)
             "allocated": float,        # EFFECTIVE allocation, 2dp
             "clamped": bool}           # True when the request was reduced

        ``allocated`` never exceeds ``available_cash``: a fixed amount larger
        than the account is reduced to the available cash and ``clamped`` is
        set, so the UI can say so rather than silently showing a smaller number.
    """
    try:
        cash = float(available_cash)
    except (TypeError, ValueError):
        cash = 0.0
    if cash < 0 or cash != cash:  # negative or NaN
        cash = 0.0

    resolved_mode = mode if mode in VALID_MODES else "percent"

    try:
        pct = float(percent)
    except (TypeError, ValueError):
        pct = 0.0
    if pct != pct:  # NaN
        pct = 0.0
    pct = max(0.0, min(100.0, pct))

    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = 0.0
    if amt < 0 or amt != amt:  # negative or NaN
        amt = 0.0

    if resolved_mode == "amount":
        requested = amt
    else:
        requested = cash * (pct / 100.0)

    clamped = requested > cash
    allocated = cash if clamped else requested

    return {
        "available_cash": round(cash, 2),
        "mode": resolved_mode,
        "percent": round(pct, 2),
        "amount": round(amt, 2),
        "allocated": round(allocated, 2),
        "clamped": clamped,
    }


def parse_funds(funds_data):
    """Extract the numeric capital fields from a broker funds payload (pure).

    Broker adapters return these as preformatted strings (e.g. ``"12345.67"``),
    so every field is coerced defensively; anything missing or unparseable
    becomes 0.0 rather than breaking the settings page.

    Args:
        funds_data: The ``data`` dict from ``get_funds`` (or None).

    Returns:
        ``{"availablecash", "collateral", "utiliseddebits", "m2mrealized",
        "m2munrealized"}`` as floats.
    """
    out = {}
    keys = (
        "availablecash",
        "collateral",
        "utiliseddebits",
        "m2mrealized",
        "m2munrealized",
    )
    source = funds_data if isinstance(funds_data, dict) else {}
    for key in keys:
        try:
            out[key] = float(source.get(key, 0) or 0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


# ---------------------------------------------------------------------------
# IO shell (broker + config access, guarded - never raises to the caller)
# ---------------------------------------------------------------------------


def get_capital_status(user_id):
    """Return live broker capital merged with the saved allocation.

    Reads the broker's funds through ``get_funds`` (which transparently returns
    sandbox funds in analyzer mode) and the persisted allocation from the shared
    risk config, then resolves the effective allocation.

    Args:
        user_id: OpenAlgo session user id.

    Returns:
        ``{"status": "success", "data": {...}}`` with ``funds``, the resolved
        allocation fields, and ``mode`` (live/analyzer); or
        ``{"status": "error", "message": ...}``. Never raises.
    """
    try:
        from database.auth_db import get_api_key_for_tradingview
        from database.settings_db import get_analyze_mode
        from services.funds_service import get_funds
        from services.risk_monitor_service import load_risk_config

        cfg = load_risk_config()
        analyze = bool(get_analyze_mode())

        api_key = None
        try:
            api_key = get_api_key_for_tradingview(user_id)
        except Exception:
            api_key = None

        funds = {}
        funds_error = None
        if api_key:
            try:
                success, response, _status = get_funds(api_key=api_key)
                if success and isinstance(response, dict):
                    funds = parse_funds(response.get("data"))
                else:
                    funds_error = "Broker did not return funds"
            except Exception as e:
                logger.exception(f"Error fetching broker funds for capital: {e}")
                funds_error = "Could not reach the broker"
        else:
            funds_error = "No broker session"

        resolved = resolve_allocation(
            funds.get("availablecash", 0.0),
            cfg.get("capital_mode"),
            cfg.get("capital_amount"),
            cfg.get("capital_percent"),
        )

        # resolve_allocation returns its own "mode" (percent/amount); surface it
        # as capital_mode so it cannot collide with the live/analyzer mode below.
        allocation = dict(resolved)
        allocation["capital_mode"] = allocation.pop("mode")

        data = {
            "funds": funds,
            # Distinguish "broker says zero" from "we could not ask" so the UI
            # shows an honest error instead of a confident zero allocation.
            "funds_error": funds_error,
            "mode": "analyzer" if analyze else "live",
            **allocation,
        }
        return {"status": "success", "data": data}
    except Exception as e:
        logger.exception(f"Error building capital status: {e}")
        return {"status": "error", "message": str(e)}


def update_capital_allocation(mode=None, amount=None, percent=None):
    """Persist the allocation config, then return the refreshed status inputs.

    Only the provided fields are changed. Validation mirrors the pure resolver:
    an unknown mode is rejected, and negative / out-of-range numbers are
    refused rather than silently coerced, so a bad request cannot quietly
    change how much capital is allocated.

    Args:
        mode: ``"percent"`` or ``"amount"`` (optional).
        amount: Fixed rupee allocation (optional, must be >= 0).
        percent: Percent of available cash (optional, must be 0-100).

    Returns:
        ``{"status": "success", "data": <saved config subset>}`` or
        ``{"status": "error", "message": ...}``.
    """
    try:
        from services.risk_monitor_service import update_risk_config

        updates = {}
        if mode is not None:
            if mode not in VALID_MODES:
                return {
                    "status": "error",
                    "message": f"mode must be one of {', '.join(VALID_MODES)}",
                }
            updates["capital_mode"] = mode
        if amount is not None:
            try:
                amt = float(amount)
            except (TypeError, ValueError):
                return {"status": "error", "message": "amount must be a number"}
            if amt < 0:
                return {"status": "error", "message": "amount must not be negative"}
            updates["capital_amount"] = amt
        if percent is not None:
            try:
                pct = float(percent)
            except (TypeError, ValueError):
                return {"status": "error", "message": "percent must be a number"}
            if pct < 0 or pct > 100:
                return {"status": "error", "message": "percent must be between 0 and 100"}
            updates["capital_percent"] = pct

        if not updates:
            return {"status": "error", "message": "nothing to update"}

        cfg = update_risk_config(**updates)
        return {
            "status": "success",
            "data": {
                "capital_mode": cfg.get("capital_mode"),
                "capital_amount": cfg.get("capital_amount"),
                "capital_percent": cfg.get("capital_percent"),
            },
        }
    except Exception as e:
        logger.exception(f"Error updating capital allocation: {e}")
        return {"status": "error", "message": str(e)}
