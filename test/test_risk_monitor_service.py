"""Offline unit tests for the daily-loss circuit breaker.

Covers the pure decision core (``should_halt``), the running-strategy selector
(``select_strategies_to_stop``), and the atomic config round-trip
(load/save/update/reset) against a tmp path. No DB, no network -- this is
safety-critical risk logic, so the branches are pinned exactly.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test")
os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import logging

import services.risk_monitor_service as rms
from services.risk_monitor_service import (
    _DEFAULTS,
    _stop_all_running,
    get_risk_status,
    load_risk_config,
    reset_halt,
    run_risk_check,
    save_risk_config,
    select_strategies_to_stop,
    should_halt,
    update_risk_config,
)

# ---------------------------------------------------------------------------
# should_halt
# ---------------------------------------------------------------------------


class TestShouldHalt:
    def test_below_limit_halts(self):
        # Loss worse than the limit -> halt.
        assert should_halt(-6000, enabled=True, daily_loss_limit=5000, already_halted=False)

    def test_at_limit_halts(self):
        # Exactly at the negative limit -> halt (<=).
        assert should_halt(-5000, enabled=True, daily_loss_limit=5000, already_halted=False)

    def test_above_limit_no_halt(self):
        # Loss smaller than the limit -> no halt.
        assert not should_halt(-4999, enabled=True, daily_loss_limit=5000, already_halted=False)

    def test_profit_no_halt(self):
        assert not should_halt(10000, enabled=True, daily_loss_limit=5000, already_halted=False)

    def test_disabled_never_halts(self):
        assert not should_halt(-100000, enabled=False, daily_loss_limit=5000, already_halted=False)

    def test_already_halted_no_reaction(self):
        # Idempotent: once halted, should_halt returns False.
        assert not should_halt(-100000, enabled=True, daily_loss_limit=5000, already_halted=True)

    def test_zero_limit_disabled(self):
        assert not should_halt(-100000, enabled=True, daily_loss_limit=0, already_halted=False)

    def test_negative_limit_disabled(self):
        # A limit <= 0 means disabled -> never an instant halt.
        assert not should_halt(-100000, enabled=True, daily_loss_limit=-5000, already_halted=False)

    def test_sign_handling_positive_limit_magnitude(self):
        # daily_loss_limit is a magnitude; a positive value guards a negative pnl.
        assert should_halt(-5001, enabled=True, daily_loss_limit=5000, already_halted=False)
        assert not should_halt(-4999, enabled=True, daily_loss_limit=5000, already_halted=False)

    def test_non_numeric_pnl_safe(self):
        assert not should_halt("bad", enabled=True, daily_loss_limit=5000, already_halted=False)


# ---------------------------------------------------------------------------
# select_strategies_to_stop
# ---------------------------------------------------------------------------


class TestSelectStrategiesToStop:
    def test_only_running(self):
        configs = {
            "a": {"is_running": True},
            "b": {"is_running": False},
            "c": {"is_running": True},
            "d": {},
        }
        assert select_strategies_to_stop(configs) == ["a", "c"]

    def test_deterministic_sorted_order(self):
        configs = {
            "zeta": {"is_running": True},
            "alpha": {"is_running": True},
            "mike": {"is_running": True},
        }
        assert select_strategies_to_stop(configs) == ["alpha", "mike", "zeta"]

    def test_empty(self):
        assert select_strategies_to_stop({}) == []
        assert select_strategies_to_stop(None) == []

    def test_ignores_non_dict_values(self):
        configs = {"a": {"is_running": True}, "bad": "not-a-dict"}
        assert select_strategies_to_stop(configs) == ["a"]


# ---------------------------------------------------------------------------
# Config load/save/update/reset round-trip (tmp path)
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / "risk_config.json"


class TestConfigRoundTrip:
    def test_load_missing_returns_defaults(self, cfg_path):
        cfg = load_risk_config(cfg_path)
        assert cfg == _DEFAULTS
        assert cfg is not _DEFAULTS  # a copy, not the shared default

    def test_load_corrupt_returns_defaults(self, cfg_path):
        cfg_path.write_text("{ not valid json")
        cfg = load_risk_config(cfg_path)
        assert cfg["enabled"] is False
        assert cfg["daily_loss_limit"] == 5000.0

    def test_save_then_load(self, cfg_path):
        saved = save_risk_config(
            {"enabled": True, "daily_loss_limit": 12000, "flatten_on_halt": True},
            cfg_path,
        )
        assert saved is True
        assert cfg_path.exists()
        cfg = load_risk_config(cfg_path)
        assert cfg["enabled"] is True
        assert cfg["daily_loss_limit"] == 12000.0
        assert cfg["flatten_on_halt"] is True

    def test_update_valid(self, cfg_path):
        cfg = update_risk_config(config_path=cfg_path, enabled=True, daily_loss_limit=7500)
        assert cfg["enabled"] is True
        assert cfg["daily_loss_limit"] == 7500.0
        # Persisted.
        assert load_risk_config(cfg_path)["daily_loss_limit"] == 7500.0

    def test_update_rejects_zero_limit(self, cfg_path):
        with pytest.raises(ValueError):
            update_risk_config(config_path=cfg_path, daily_loss_limit=0)

    def test_update_rejects_negative_limit(self, cfg_path):
        with pytest.raises(ValueError):
            update_risk_config(config_path=cfg_path, daily_loss_limit=-100)

    def test_update_rejects_non_numeric_limit(self, cfg_path):
        with pytest.raises(ValueError):
            update_risk_config(config_path=cfg_path, daily_loss_limit="lots")

    def test_reset_clears_halt(self, cfg_path):
        save_risk_config(
            {
                "enabled": True,
                "daily_loss_limit": 5000,
                "halted": True,
                "halted_at": "2026-07-27T10:00:00+05:30",
                "halted_reason": "breach",
                "halted_pnl": -6000,
            },
            cfg_path,
        )
        cfg = reset_halt(cfg_path)
        assert cfg["halted"] is False
        assert cfg["halted_at"] is None
        assert cfg["halted_reason"] is None
        assert cfg["halted_pnl"] is None
        # Enabled/limit preserved across a reset (re-armed, not disabled).
        assert cfg["enabled"] is True
        assert cfg["daily_loss_limit"] == 5000.0

    def test_get_risk_status_breaching(self, cfg_path):
        save_risk_config({"enabled": True, "daily_loss_limit": 5000}, cfg_path)
        status = get_risk_status(-6000, cfg_path)
        assert status["breaching"] is True
        assert status["day_pnl"] == -6000.0
        assert status["enabled"] is True

    def test_get_risk_status_not_breaching(self, cfg_path):
        save_risk_config({"enabled": True, "daily_loss_limit": 5000}, cfg_path)
        status = get_risk_status(2000, cfg_path)
        assert status["breaching"] is False

    def test_get_risk_status_disabled_not_breaching(self, cfg_path):
        save_risk_config({"enabled": False, "daily_loss_limit": 5000}, cfg_path)
        status = get_risk_status(-99999, cfg_path)
        assert status["breaching"] is False


# ---------------------------------------------------------------------------
# MAJOR 3: update_risk_config must preserve a fresh on-disk halted latch
# ---------------------------------------------------------------------------


class TestUpdatePreservesLatch:
    def test_update_preserves_ondisk_halted_latch(self, cfg_path):
        # Monitor has latched halted on disk.
        save_risk_config(
            {
                "enabled": True,
                "daily_loss_limit": 5000,
                "halted": True,
                "halted_at": "2026-07-27T10:00:00+05:30",
                "halted_reason": "breach",
                "halted_pnl": -6000,
            },
            cfg_path,
        )
        # An operator POST changing only the limit must NOT clear halted.
        cfg = update_risk_config(config_path=cfg_path, daily_loss_limit=8000)
        assert cfg["halted"] is True
        assert cfg["halted_reason"] == "breach"
        assert cfg["halted_pnl"] == -6000.0
        assert cfg["daily_loss_limit"] == 8000.0
        # And it is persisted (freshest read wins).
        on_disk = load_risk_config(cfg_path)
        assert on_disk["halted"] is True
        assert on_disk["daily_loss_limit"] == 8000.0

    def test_update_toggle_enabled_preserves_latch(self, cfg_path):
        save_risk_config(
            {"enabled": True, "daily_loss_limit": 5000, "halted": True}, cfg_path
        )
        cfg = update_risk_config(config_path=cfg_path, enabled=False)
        assert cfg["enabled"] is False
        assert cfg["halted"] is True


# ---------------------------------------------------------------------------
# MAJOR 4: corrupt config fails open LOUDLY (ERROR log), returns safe defaults
# ---------------------------------------------------------------------------


class TestCorruptConfigLoud:
    def test_corrupt_config_logs_error_and_returns_defaults(self, cfg_path, caplog):
        cfg_path.write_text("{ this is : not json ]")
        with caplog.at_level(logging.ERROR):
            cfg = load_risk_config(cfg_path)
        assert cfg == _DEFAULTS
        # The failure must be visible at ERROR (not a silent debug line).
        assert any(rec.levelno >= logging.ERROR for rec in caplog.records)
        assert any("CORRUPT risk config" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# CRITICAL 2: stop path reconciles on the authoritative running set
# ---------------------------------------------------------------------------


class TestStopAllRunningReconciliation:
    def test_stops_authoritative_running_ids_sorted(self):
        calls = []
        stopped = _stop_all_running(lambda sid: calls.append(sid), running_ids=["c", "a", "b"])
        assert stopped == ["a", "b", "c"]
        assert calls == ["a", "b", "c"]

    def test_dedups_running_ids(self):
        calls = []
        stopped = _stop_all_running(lambda sid: calls.append(sid), running_ids=["a", "a", "b"])
        assert stopped == ["a", "b"]

    def test_continues_when_one_stop_throws(self):
        def flaky(sid):
            if sid == "a":
                raise RuntimeError("stop failed")

        stopped = _stop_all_running(flaky, running_ids=["a", "b"])
        # "a" threw so it's not counted, but "b" is still stopped.
        assert stopped == ["b"]

    def test_run_risk_check_stops_authoritative_set_not_config_flag(self, tmp_path, monkeypatch):
        # A live-by-PID strategy whose config flag is stale-False must still be
        # stopped: run_risk_check must use the passed running_ids, not the
        # config-flag selector.
        cfg_file = tmp_path / "risk_config.json"
        monkeypatch.setattr(rms, "RISK_CONFIG_FILE", cfg_file)
        save_risk_config({"enabled": True, "daily_loss_limit": 5000}, cfg_file)
        monkeypatch.setattr(rms, "compute_portfolio_day_pnl", lambda uid: -9000.0)

        stopped_calls = []
        result = run_risk_check(
            "admin",
            stop_fn=lambda sid: stopped_calls.append(sid),
            running_ids=["live_by_pid"],
        )
        assert result["action"] == "halted"
        assert stopped_calls == ["live_by_pid"]
        assert result["stopped"] == ["live_by_pid"]
        # Latch persisted.
        assert load_risk_config(cfg_file)["halted"] is True

    def test_run_risk_check_no_halt_above_limit(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "risk_config.json"
        monkeypatch.setattr(rms, "RISK_CONFIG_FILE", cfg_file)
        save_risk_config({"enabled": True, "daily_loss_limit": 5000}, cfg_file)
        monkeypatch.setattr(rms, "compute_portfolio_day_pnl", lambda uid: -100.0)
        stopped_calls = []
        result = run_risk_check(
            "admin", stop_fn=lambda sid: stopped_calls.append(sid), running_ids=["x"]
        )
        assert result["action"] == "ok"
        assert stopped_calls == []
        assert load_risk_config(cfg_file)["halted"] is False
