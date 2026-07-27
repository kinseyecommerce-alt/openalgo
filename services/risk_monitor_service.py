"""Server-side daily-loss circuit breaker for autonomous trading.

A risk monitor that halts every running Python strategy when the account's
intraday portfolio P&L breaches a configured daily-loss limit -- regardless of
whether any browser is open. The scheduler (``blueprints/python_strategy.py``)
runs :func:`run_risk_check` once a minute; the decision logic is a **pure**
tested function so the safety-critical branch can be verified offline.

Design rules (safety-critical):
- The decision (:func:`should_halt`) and selection (:func:`select_strategies_to_stop`)
  cores take no DB / network / clock -- pure functions, unit tested.
- Config persistence is atomic (tmp + ``os.replace``), mirroring
  ``blueprints/python_strategy.save_configs``.
- Every IO / job entrypoint is wrapped so a failure logs via
  ``logger.exception`` and NEVER propagates to the APScheduler thread.
- The breaker is idempotent: once ``halted`` is set it takes no further action
  until :func:`reset_halt` re-arms it.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Persisted risk-breaker state. Lives beside strategies/strategy_configs.json.
RISK_CONFIG_FILE = Path("strategies") / "risk_config.json"

# Serializes every load-modify-write sequence on the risk config so a
# concurrent POST /api/risk cannot clobber the monitor's halt latch (and vice
# versa). Held around update_risk_config / reset_halt / run_risk_check's
# halt-latch write. Reentrant so nested helpers can re-acquire safely.
_CONFIG_LOCK = threading.RLock()

# Defaults for a fresh / missing / corrupt config file.
_DEFAULTS = {
    "enabled": False,
    "daily_loss_limit": 5000.0,
    "flatten_on_halt": False,
    "halted": False,
    "halted_at": None,
    "halted_reason": None,
    "halted_pnl": None,
}


# ---------------------------------------------------------------------------
# Pure core (NO db, NO network, NO clock) - unit tested offline
# ---------------------------------------------------------------------------


def should_halt(
    portfolio_day_pnl: float,
    enabled: bool,
    daily_loss_limit: float,
    already_halted: bool,
) -> bool:
    """Decide whether the circuit breaker must trip now.

    Args:
        portfolio_day_pnl: Full-account intraday P&L (negative = loss).
        enabled: Whether the breaker is armed.
        daily_loss_limit: Positive rupee magnitude of the max tolerated day
            loss. A value ``<= 0`` means the breaker is effectively disabled.
        already_halted: Whether the breaker has already tripped (idempotency).

    Returns:
        ``True`` iff the breaker is enabled, not already halted, the limit is a
        positive magnitude, and the portfolio day P&L is at or below the
        negative limit (``portfolio_day_pnl <= -abs(daily_loss_limit)``).
    """
    if not enabled or already_halted:
        return False
    try:
        limit = float(daily_loss_limit)
        pnl = float(portfolio_day_pnl)
    except (TypeError, ValueError):
        return False
    if limit <= 0:
        # A non-positive limit is treated as "disabled", never an instant halt.
        return False
    return pnl <= -abs(limit)


def select_strategies_to_stop(configs: dict) -> list[str]:
    """Return ids of strategies currently running, in deterministic order.

    Args:
        configs: Mapping of ``strategy_id -> config_dict``. A strategy counts as
            running when ``config.get('is_running')`` is truthy.

    Returns:
        Sorted list of running strategy ids (sorted for deterministic behavior).
    """
    if not configs:
        return []
    running = [
        sid
        for sid, cfg in configs.items()
        if isinstance(cfg, dict) and cfg.get("is_running")
    ]
    return sorted(running)


# ---------------------------------------------------------------------------
# Config persistence (atomic file IO, never raises)
# ---------------------------------------------------------------------------


def _coerce_config(raw: dict) -> dict:
    """Merge a raw dict onto defaults, coercing types defensively."""
    cfg = dict(_DEFAULTS)
    if isinstance(raw, dict):
        cfg["enabled"] = bool(raw.get("enabled", _DEFAULTS["enabled"]))
        cfg["flatten_on_halt"] = bool(
            raw.get("flatten_on_halt", _DEFAULTS["flatten_on_halt"])
        )
        cfg["halted"] = bool(raw.get("halted", _DEFAULTS["halted"]))
        try:
            cfg["daily_loss_limit"] = float(
                raw.get("daily_loss_limit", _DEFAULTS["daily_loss_limit"])
            )
        except (TypeError, ValueError):
            cfg["daily_loss_limit"] = _DEFAULTS["daily_loss_limit"]
        cfg["halted_at"] = raw.get("halted_at")
        cfg["halted_reason"] = raw.get("halted_reason")
        hp = raw.get("halted_pnl")
        try:
            cfg["halted_pnl"] = float(hp) if hp is not None else None
        except (TypeError, ValueError):
            cfg["halted_pnl"] = None
    return cfg


def load_risk_config(config_path: Path | str | None = None) -> dict:
    """Load the risk config, returning defaults if absent or corrupt.

    Never raises. A missing or malformed file yields a fresh default config.

    Args:
        config_path: Optional override path (used by tests). Defaults to
            :data:`RISK_CONFIG_FILE`.

    Returns:
        A fully-populated config dict.
    """
    path = Path(config_path) if config_path else RISK_CONFIG_FILE
    if not path.exists():
        return dict(_DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return _coerce_config(raw)
    except Exception as e:
        # Loud on purpose: a corrupt/unreadable config makes the breaker fail
        # OPEN (defaults: disabled, not halted). This must be visible in the
        # error log, never a silent debug line.
        logger.exception(
            f"CORRUPT risk config at {path} - circuit breaker FAILING OPEN "
            f"(returning safe defaults, breaker disabled): {e}"
        )
        return dict(_DEFAULTS)


def save_risk_config(cfg: dict, config_path: Path | str | None = None) -> bool:
    """Persist the risk config atomically (tmp + ``os.replace``).

    Mirrors ``blueprints/python_strategy.save_configs`` so a kill mid-write
    cannot leave a half-written JSON blob behind. Never raises.

    Args:
        cfg: Config dict to persist (coerced onto defaults first).
        config_path: Optional override path (used by tests).

    Returns:
        ``True`` on success, ``False`` if the write failed.
    """
    path = Path(config_path) if config_path else RISK_CONFIG_FILE
    try:
        normalized = _coerce_config(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logger.exception(f"Failed to save risk config: {e}")
        return False


def update_risk_config(config_path: Path | str | None = None, **fields) -> dict:
    """Validate and apply the given fields, persist, and return the new config.

    Accepts ``enabled`` (bool), ``daily_loss_limit`` (numeric > 0), and
    ``flatten_on_halt`` (bool). Unknown fields are ignored. Invalid values
    raise ``ValueError`` (the route turns this into a 4xx).

    Args:
        config_path: Optional override path (used by tests).
        **fields: Any of ``enabled``, ``daily_loss_limit``, ``flatten_on_halt``.

    Returns:
        The updated config dict.

    Raises:
        ValueError: If ``daily_loss_limit`` is non-numeric or ``<= 0``.
    """
    # Validate BEFORE taking the lock so a bad request never blocks the monitor.
    if "daily_loss_limit" in fields:
        try:
            limit = float(fields["daily_loss_limit"])
        except (TypeError, ValueError) as e:
            raise ValueError("daily_loss_limit must be a number") from e
        if limit <= 0:
            raise ValueError("daily_loss_limit must be greater than 0")
    else:
        limit = None

    with _CONFIG_LOCK:
        # Re-read the freshest on-disk config UNDER the lock so we never write a
        # stale halted latch: only the operator-settable fields are mutated;
        # halted/halted_* are preserved from the newest read (which may have
        # been latched by a concurrent monitor halt).
        cfg = load_risk_config(config_path)

        if limit is not None:
            cfg["daily_loss_limit"] = limit
        if "enabled" in fields:
            cfg["enabled"] = bool(fields["enabled"])
        if "flatten_on_halt" in fields:
            cfg["flatten_on_halt"] = bool(fields["flatten_on_halt"])

        save_risk_config(cfg, config_path)
        return cfg


def reset_halt(config_path: Path | str | None = None) -> dict:
    """Clear the halted state, re-arming the breaker. Never raises.

    Args:
        config_path: Optional override path (used by tests).

    Returns:
        The updated config dict with halt fields cleared.
    """
    with _CONFIG_LOCK:
        cfg = load_risk_config(config_path)
        cfg["halted"] = False
        cfg["halted_at"] = None
        cfg["halted_reason"] = None
        cfg["halted_pnl"] = None
        save_risk_config(cfg, config_path)
        return cfg


def get_risk_status(
    portfolio_day_pnl: float, config_path: Path | str | None = None
) -> dict:
    """Return the config plus live P&L and a computed ``breaching`` flag.

    ``breaching`` reflects whether the live P&L is currently at/below the
    negative limit (independent of the ``halted`` latch), so the dashboard can
    warn before a trip and show why a halt fired.

    Args:
        portfolio_day_pnl: Full-account intraday P&L.
        config_path: Optional override path (used by tests).

    Returns:
        A dict: the config fields, ``day_pnl``, and ``breaching`` (bool).
    """
    cfg = load_risk_config(config_path)
    try:
        pnl = float(portfolio_day_pnl)
    except (TypeError, ValueError):
        pnl = 0.0
    limit = cfg.get("daily_loss_limit", 0.0)
    breaching = bool(cfg.get("enabled")) and limit > 0 and pnl <= -abs(limit)
    status = dict(cfg)
    status["day_pnl"] = round(pnl, 2)
    status["breaching"] = breaching
    return status


# ---------------------------------------------------------------------------
# Monitor job (IO shell) - wrapped so it never crashes the scheduler
# ---------------------------------------------------------------------------


def compute_portfolio_day_pnl(user_id: str) -> float | None:
    """Full-account intraday P&L = totals.day_pnl + totals.untagged_day_pnl.

    Reuses ``services.strategy_pnl_service.get_strategy_pnl`` (do NOT
    reimplement P&L math). Returns ``None`` on any failure so the caller can
    short-circuit without acting on a bad number.

    Args:
        user_id: OpenAlgo session user id.

    Returns:
        The portfolio intraday P&L as a float, or ``None`` if it could not be
        computed.
    """
    try:
        from services.strategy_pnl_service import get_strategy_pnl

        result = get_strategy_pnl(user_id)
        if not result or result.get("status") != "success":
            return None
        totals = (result.get("data") or {}).get("totals") or {}
        day = float(totals.get("day_pnl", 0) or 0)
        untagged = float(totals.get("untagged_day_pnl", 0) or 0)
        return day + untagged
    except Exception as e:
        logger.exception(f"Error computing portfolio day P&L: {e}")
        return None


def run_risk_check(
    user_id, stop_fn, flatten_fn=None, notify_fn=None, running_ids=None
) -> dict:
    """Run one circuit-breaker check. Idempotent; never raises.

    Loads config; if disabled or already halted, returns a no-action status
    dict. Otherwise computes the portfolio intraday P&L and, if
    :func:`should_halt` fires, stops every running strategy (each marked so it
    will NOT auto-restart), latches ``halted`` state, optionally flattens
    positions, best-effort notifies, and emits a ``risk_halt`` SocketIO event.

    Args:
        user_id: OpenAlgo session user id to compute P&L for.
        stop_fn: ``callable(strategy_id) -> None`` that stops a strategy AND
            marks it manually-stopped (so the scheduler won't auto-restart it).
        flatten_fn: Optional ``callable() -> None`` to close all positions,
            called only when ``flatten_on_halt`` is set.
        notify_fn: Optional ``callable(message: str) -> None`` best-effort
            alert; a failure is logged, never fatal.
        running_ids: Optional AUTHORITATIVE list of strategy ids that are
            ACTUALLY running (by live process/PID state, not the possibly-stale
            ``config['is_running']`` flag). When provided, every id in it is
            stopped on halt. When ``None``, the stop path falls back to
            :func:`select_strategies_to_stop` over ``STRATEGY_CONFIGS``.

    Returns:
        A status dict describing what happened (``action`` in
        ``{"disabled", "already_halted", "no_pnl", "ok", "halted", "error"}``).
    """
    try:
        cfg = load_risk_config()

        if not cfg.get("enabled"):
            return {"action": "disabled", "halted": False}

        if cfg.get("halted"):
            return {"action": "already_halted", "halted": True}

        if not user_id:
            return {"action": "no_pnl", "halted": False, "reason": "no user"}

        portfolio_day_pnl = compute_portfolio_day_pnl(user_id)
        if portfolio_day_pnl is None:
            # Could not read P&L this tick -- take NO action (fail safe: do not
            # halt on a bad read, and do not clear anything).
            return {"action": "no_pnl", "halted": False}

        limit = cfg.get("daily_loss_limit", 0.0)
        if not should_halt(
            portfolio_day_pnl,
            enabled=bool(cfg.get("enabled")),
            daily_loss_limit=limit,
            already_halted=bool(cfg.get("halted")),
        ):
            return {
                "action": "ok",
                "halted": False,
                "day_pnl": round(portfolio_day_pnl, 2),
            }

        # --- BREACH: trip the breaker ------------------------------------
        halted_at = datetime.now(IST).isoformat()
        reason = (
            f"Daily loss limit breached: day P&L "
            f"{round(portfolio_day_pnl, 2)} <= -{abs(float(limit))}"
        )
        logger.warning(f"RISK HALT: {reason}")

        # Stop EVERY actually-running strategy. Prefer the authoritative
        # running_ids (live PID state) so a stale config['is_running']=False
        # can never leave a live strategy trading after the latch is set.
        stopped = _stop_all_running(stop_fn, running_ids=running_ids)

        # Latch halted state under the lock so a concurrent update_risk_config
        # cannot read-modify-write over the latch we are about to set.
        with _CONFIG_LOCK:
            latch = load_risk_config()
            latch["halted"] = True
            latch["halted_at"] = halted_at
            latch["halted_reason"] = reason
            latch["halted_pnl"] = round(portfolio_day_pnl, 2)
            save_risk_config(latch)
            cfg = latch

        # Optional flatten -- only when configured, never fatal.
        if cfg.get("flatten_on_halt") and flatten_fn is not None:
            try:
                flatten_fn()
                logger.warning("RISK HALT: flatten_on_halt executed")
            except Exception as e:
                logger.exception(f"RISK HALT: flatten_fn failed: {e}")

        # Best-effort notify -- never fatal.
        if notify_fn is not None:
            try:
                notify_fn(reason)
            except Exception as e:
                logger.exception(f"RISK HALT: notify_fn failed: {e}")

        # Emit SocketIO event -- never fatal.
        _emit_risk_halt(
            reason=reason,
            day_pnl=round(portfolio_day_pnl, 2),
            limit=abs(float(limit)),
            halted_at=halted_at,
            stopped=stopped,
        )

        return {
            "action": "halted",
            "halted": True,
            "day_pnl": round(portfolio_day_pnl, 2),
            "stopped": stopped,
        }
    except Exception as e:
        # A failure here must never propagate to the APScheduler thread.
        logger.exception(f"run_risk_check failed: {e}")
        return {"action": "error", "halted": False}


def _stop_all_running(stop_fn, running_ids=None) -> list[str]:
    """Stop every running strategy via ``stop_fn``, guarding each call.

    Args:
        stop_fn: ``callable(strategy_id) -> None``.
        running_ids: Authoritative list of actually-running ids (live PID
            state). When ``None``, falls back to the config-flag selector over
            ``STRATEGY_CONFIGS``.
    """
    stopped = []
    if running_ids is not None:
        # Authoritative live set - deterministic order, deduplicated.
        ids = sorted(set(running_ids))
    else:
        try:
            from blueprints.python_strategy import STRATEGY_CONFIGS

            ids = select_strategies_to_stop(STRATEGY_CONFIGS)
        except Exception as e:
            logger.exception(f"RISK HALT: could not enumerate strategies: {e}")
            return stopped

    for sid in ids:
        try:
            stop_fn(sid)
            stopped.append(sid)
        except Exception as e:
            logger.exception(f"RISK HALT: failed to stop strategy {sid}: {e}")
    return stopped


def _emit_risk_halt(reason, day_pnl, limit, halted_at, stopped):
    """Emit the ``risk_halt`` SocketIO event, best-effort."""
    try:
        from extensions import socketio

        socketio.emit(
            "risk_halt",
            {
                "reason": reason,
                "day_pnl": day_pnl,
                "limit": limit,
                "halted_at": halted_at,
                "stopped": stopped,
            },
        )
    except Exception as e:
        logger.exception(f"RISK HALT: socketio emit failed: {e}")
