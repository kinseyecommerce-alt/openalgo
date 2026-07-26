"""Offline unit tests for the deployment automation pure helpers.

deploy_strategies keeps build_config / find_existing / merge_registration and
the input validators free of network, Flask, and filesystem dependencies, so
these tests run without a broker session, a running server, or market hours.
"""

import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies")
)

from deploy_strategies import (
    STRATEGY_STEMS,
    build_config,
    find_existing,
    merge_registration,
    validate_days,
    validate_hhmm,
)

REQUIRED_KEYS = {
    "name",
    "file_path",
    "file_name",
    "exchange",
    "is_running",
    "is_scheduled",
    "created_at",
    "user_id",
    "schedule_start",
    "schedule_stop",
    "schedule_days",
}


def make_config(strategy_id, stem="rsi_meanreversion_strategy", **overrides):
    config = build_config(
        name=stem,
        strategy_id=strategy_id,
        user_id="testuser",
        exchange="NSE",
        schedule_start="09:20",
        schedule_stop="15:20",
        schedule_days=["mon", "tue", "wed", "thu", "fri"],
        created_at="2026-07-26T10:00:00+05:30",
    )
    config.update(overrides)
    return config


class TestBuildConfig:
    def test_all_required_keys_present(self):
        config = make_config("rsi_meanreversion_strategy_20260726100000")
        assert set(config.keys()) == REQUIRED_KEYS

    def test_scheduled_true_running_false(self):
        config = make_config("rsi_meanreversion_strategy_20260726100000")
        assert config["is_scheduled"] is True
        assert config["is_running"] is False

    def test_file_name_and_path_match_strategy_id(self):
        strategy_id = "four_ema_retracement_strategy_20260726100000"
        config = make_config(strategy_id, stem="four_ema_retracement_strategy")
        assert config["file_name"] == f"{strategy_id}.py"
        assert config["file_path"] == os.path.join("strategies", "scripts", f"{strategy_id}.py")

    def test_exchange_uppercased(self):
        config = make_config("rsi_meanreversion_strategy_20260726100000", exchange="NSE")
        assert config["exchange"] == "NSE"
        config = build_config(
            name="x",
            strategy_id="x_20260726100000",
            user_id="u",
            exchange="nse",
            schedule_start="09:20",
            schedule_stop="15:20",
            schedule_days=["mon"],
            created_at="2026-07-26T10:00:00+05:30",
        )
        assert config["exchange"] == "NSE"

    def test_schedule_fields_carried_through(self):
        config = make_config("rsi_meanreversion_strategy_20260726100000")
        assert config["user_id"] == "testuser"
        assert config["schedule_start"] == "09:20"
        assert config["schedule_stop"] == "15:20"
        assert config["schedule_days"] == ["mon", "tue", "wed", "thu", "fri"]

    def test_known_stems(self):
        assert STRATEGY_STEMS == (
            "rsi_meanreversion_strategy",
            "four_ema_retracement_strategy",
        )


class TestFindExisting:
    def test_finds_entry_by_stem_prefix(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        configs = {old_id: make_config(old_id)}
        assert find_existing(configs, "rsi_meanreversion_strategy") == old_id

    def test_does_not_match_other_stems(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        configs = {old_id: make_config(old_id)}
        assert find_existing(configs, "four_ema_retracement_strategy") is None

    def test_empty_configs(self):
        assert find_existing({}, "rsi_meanreversion_strategy") is None


class TestMergeRegistration:
    def test_deploy_into_empty(self):
        new_id = "rsi_meanreversion_strategy_20260726100000"
        config = make_config(new_id)
        merged, action = merge_registration({}, "rsi_meanreversion_strategy", new_id, config)
        assert action == "deployed"
        assert merged == {new_id: config}

    def test_idempotent_skip_on_existing_stem(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        configs = {old_id: make_config(old_id)}
        new_id = "rsi_meanreversion_strategy_20260726100000"
        merged, action = merge_registration(
            configs, "rsi_meanreversion_strategy", new_id, make_config(new_id)
        )
        assert action == "skipped"
        assert merged == configs
        assert new_id not in merged

    def test_force_replaces_exactly_the_old_entry(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        foreign_id = "my_custom_strategy_20260101090000"
        configs = {
            old_id: make_config(old_id),
            foreign_id: make_config(foreign_id, stem="my_custom_strategy"),
        }
        new_id = "rsi_meanreversion_strategy_20260726100000"
        new_config = make_config(new_id)
        merged, action = merge_registration(
            configs, "rsi_meanreversion_strategy", new_id, new_config, force=True
        )
        assert action == "replaced"
        assert old_id not in merged
        assert merged[new_id] == new_config
        assert set(merged.keys()) == {new_id, foreign_id}

    def test_foreign_entries_preserved_byte_for_byte(self):
        foreign_id = "my_custom_strategy_20260101090000"
        foreign = make_config(foreign_id, stem="my_custom_strategy")
        before = json.dumps(foreign, sort_keys=True)
        configs = {foreign_id: foreign}
        new_id = "rsi_meanreversion_strategy_20260726100000"
        merged, action = merge_registration(
            configs, "rsi_meanreversion_strategy", new_id, make_config(new_id), force=True
        )
        assert action == "deployed"
        assert merged[foreign_id] is foreign
        assert json.dumps(merged[foreign_id], sort_keys=True) == before

    def test_input_mapping_not_mutated(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        configs = {old_id: make_config(old_id)}
        snapshot = json.dumps(configs, sort_keys=True)
        new_id = "rsi_meanreversion_strategy_20260726100000"
        merge_registration(
            configs, "rsi_meanreversion_strategy", new_id, make_config(new_id), force=True
        )
        assert json.dumps(configs, sort_keys=True) == snapshot

    def test_refuses_to_replace_running_entry(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        configs = {old_id: make_config(old_id, is_running=True)}
        new_id = "rsi_meanreversion_strategy_20260726100000"
        with pytest.raises(RuntimeError, match="is_running"):
            merge_registration(
                configs, "rsi_meanreversion_strategy", new_id, make_config(new_id), force=True
            )

    def test_running_entry_without_force_is_skipped_not_modified(self):
        old_id = "rsi_meanreversion_strategy_20260101090000"
        configs = {old_id: make_config(old_id, is_running=True)}
        new_id = "rsi_meanreversion_strategy_20260726100000"
        merged, action = merge_registration(
            configs, "rsi_meanreversion_strategy", new_id, make_config(new_id)
        )
        assert action == "skipped"
        assert merged == configs


class TestValidateHhmm:
    @pytest.mark.parametrize("value", ["00:00", "09:20", "15:20", "23:59"])
    def test_valid(self, value):
        assert validate_hhmm(value) == value

    @pytest.mark.parametrize("value", ["24:00", "9:20", "09:60", "0920", "9:5", "", None, "ab:cd"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            validate_hhmm(value)


class TestValidateDays:
    def test_valid_weekdays(self):
        assert validate_days("mon,tue,wed,thu,fri") == ["mon", "tue", "wed", "thu", "fri"]

    def test_case_insensitive_and_dedup(self):
        assert validate_days("Mon, MON, sat") == ["mon", "sat"]

    @pytest.mark.parametrize("value", ["funday", "mon,notaday", "", ",", None])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            validate_days(value)
