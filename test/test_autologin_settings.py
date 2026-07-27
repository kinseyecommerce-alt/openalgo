"""Offline unit tests for the Zerodha auto-login settings helpers.

Covers the pure status computation (autologin_status_from_env) and the .env
update logic (update_env_value) without Flask. Network-free; no calls to Kite.

Security invariant under test: the status/response shape NEVER contains a
password or totp_secret value in any form (masked or not) -- only booleans,
plus a masked user_id.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test")
os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from blueprints.broker_credentials import (
    autologin_status_from_env,
    update_env_value,
)


class TestAutologinStatusFromEnv:
    FULL = {
        "ZERODHA_USER_ID": "AB1234",
        "ZERODHA_PASSWORD": "hunter2",
        "ZERODHA_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    }

    def test_all_set_configured(self):
        status = autologin_status_from_env(self.FULL)
        assert status["fields"] == {
            "user_id": True,
            "password": True,
            "totp_secret": True,
        }
        assert status["configured"] is True

    def test_empty_env_not_configured(self):
        status = autologin_status_from_env({})
        assert status["fields"] == {
            "user_id": False,
            "password": False,
            "totp_secret": False,
        }
        assert status["configured"] is False
        assert status["masked"]["user_id"] == ""

    def test_partial_not_configured(self):
        env = {"ZERODHA_USER_ID": "AB1234", "ZERODHA_PASSWORD": "hunter2"}
        status = autologin_status_from_env(env)
        assert status["fields"]["user_id"] is True
        assert status["fields"]["password"] is True
        assert status["fields"]["totp_secret"] is False
        assert status["configured"] is False

    def test_blank_value_counts_as_not_set(self):
        env = dict(self.FULL, ZERODHA_TOTP_SECRET="   ")
        status = autologin_status_from_env(env)
        assert status["fields"]["totp_secret"] is False
        assert status["configured"] is False

    def test_user_id_masked_only(self):
        status = autologin_status_from_env(self.FULL)
        # masked contains ONLY user_id, and it is masked (never the raw value).
        assert set(status["masked"].keys()) == {"user_id"}
        assert status["masked"]["user_id"] != "AB1234"
        assert status["masked"]["user_id"].startswith("AB12")

    def test_never_emits_password_or_totp_value(self):
        status = autologin_status_from_env(self.FULL)
        blob = repr(status)
        # No password/totp_secret value appears anywhere in the status shape.
        assert "hunter2" not in blob
        assert "JBSWY3DPEHPK3PXP" not in blob
        # And there are no keys carrying those secrets, masked or otherwise.
        assert "password" not in status["masked"]
        assert "totp_secret" not in status["masked"]


class TestUpdateEnvValueForAutologin:
    BASE = (
        "BROKER_API_KEY = 'apikey'\n"
        "ZERODHA_USER_ID = 'OLD1234'\n"
        "SOME_OTHER = 'keep'\n"
    )

    def test_updates_existing_user_id(self):
        out = update_env_value(self.BASE, "ZERODHA_USER_ID", "NEW9999")
        assert "ZERODHA_USER_ID = 'NEW9999'" in out
        assert "OLD1234" not in out
        # Unrelated keys untouched.
        assert "BROKER_API_KEY = 'apikey'" in out
        assert "SOME_OTHER = 'keep'" in out

    def test_appends_missing_key(self):
        out = update_env_value(self.BASE, "ZERODHA_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        assert "ZERODHA_TOTP_SECRET = 'JBSWY3DPEHPK3PXP'" in out

    def test_only_targeted_field_changes(self):
        # Simulate a body updating only password: user_id and totp untouched.
        content = self.BASE + "ZERODHA_TOTP_SECRET = 'seed'\n"
        out = update_env_value(content, "ZERODHA_PASSWORD", "newpw")
        assert "ZERODHA_PASSWORD = 'newpw'" in out
        assert "ZERODHA_USER_ID = 'OLD1234'" in out
        assert "ZERODHA_TOTP_SECRET = 'seed'" in out

    def test_explicit_clear_writes_empty(self):
        content = self.BASE + "ZERODHA_PASSWORD = 'hunter2'\n"
        out = update_env_value(content, "ZERODHA_PASSWORD", "")
        assert "ZERODHA_PASSWORD = ''" in out
        assert "hunter2" not in out

    def test_value_with_single_quote_uses_double_quotes(self):
        out = update_env_value(self.BASE, "ZERODHA_PASSWORD", "pa'ss")
        assert 'ZERODHA_PASSWORD = "pa\'ss"' in out
