"""Offline unit tests for the Zerodha auto-login pure helpers.

Covers TOTP generation, request_token extraction, checksum, credential
validation, and secret redaction -- all network-free (no calls to Kite).
"""

import os
import sys
import time

import pyotp
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("API_KEY_PEPPER", "test")
os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from broker.zerodha.api.auto_login import (
    _is_kite_host,
    extract_request_token,
    generate_totp,
    read_credentials,
    redact,
)


class TestGenerateTotp:
    SECRET = "JBSWY3DPEHPK3PXP"  # canonical base32 test seed

    def test_matches_pyotp(self):
        assert generate_totp(self.SECRET) == pyotp.TOTP(self.SECRET).now()

    def test_tolerates_spaces(self):
        spaced = "JBSW Y3DP EHPK 3PXP"
        assert generate_totp(spaced) == pyotp.TOTP(self.SECRET).now()

    def test_six_digits(self):
        code = generate_totp(self.SECRET)
        assert len(code) == 6 and code.isdigit()

    def test_empty_secret_raises(self):
        with pytest.raises(ValueError):
            generate_totp("")
        with pytest.raises(ValueError):
            generate_totp("   ")

    def test_invalid_secret_raises(self):
        with pytest.raises(ValueError):
            generate_totp("not!valid!base32")

    def test_deterministic_within_step(self):
        # Two calls in the same 30s window yield the same code.
        assert generate_totp(self.SECRET) == generate_totp(self.SECRET)

    def test_known_vector(self):
        # RFC 6238 style: verify our output equals a freshly computed TOTP at
        # the current counter (indirectly asserts we use the standard 30s/6d).
        totp = pyotp.TOTP(self.SECRET)
        now = int(time.time())
        assert generate_totp(self.SECRET) == totp.at(now)


class TestExtractRequestToken:
    def test_standard_redirect(self):
        url = "https://myapp.example/zerodha/callback?request_token=abc123&action=login&status=success"
        assert extract_request_token(url) == "abc123"

    def test_token_first_param(self):
        assert extract_request_token("https://x/cb?request_token=tok999") == "tok999"

    def test_no_token(self):
        assert extract_request_token("https://x/cb?status=error") is None

    def test_empty_and_none(self):
        assert extract_request_token("") is None
        assert extract_request_token(None) is None

    def test_malformed_url(self):
        assert extract_request_token("not a url at all") is None

    def test_empty_token_value_is_none(self):
        assert extract_request_token("https://x/cb?request_token=") is None


class TestIsKiteHost:
    def test_accepts_kite_zerodha(self):
        assert _is_kite_host("https://kite.zerodha.com/connect/login?x=1")
        assert _is_kite_host("https://api.kite.trade/session/token")

    def test_accepts_subdomains(self):
        assert _is_kite_host("https://kite.zerodha.com/oms")

    def test_rejects_off_host(self):
        assert not _is_kite_host("https://evil.example/steal?request_token=x")
        assert not _is_kite_host("https://zerodha.com.evil.example/x")

    def test_rejects_empty_and_relative(self):
        assert not _is_kite_host("")
        assert not _is_kite_host("/connect/finish")  # relative -> no host
        assert not _is_kite_host(None)


class TestReadCredentials:
    FULL = {
        "ZERODHA_USER_ID": "AB1234",
        "ZERODHA_PASSWORD": "pw",
        "ZERODHA_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        "BROKER_API_KEY": "key",
        "BROKER_API_SECRET": "secret",
    }

    def test_all_present(self):
        creds = read_credentials(self.FULL)
        assert creds["user_id"] == "AB1234"
        assert creds["api_key"] == "key"
        assert creds["totp_secret"] == "JBSWY3DPEHPK3PXP"

    def test_strips_whitespace(self):
        env = dict(self.FULL, ZERODHA_USER_ID="  AB1234  ")
        assert read_credentials(env)["user_id"] == "AB1234"

    def test_missing_one_lists_it(self):
        env = dict(self.FULL)
        del env["ZERODHA_TOTP_SECRET"]
        with pytest.raises(ValueError) as exc:
            read_credentials(env)
        assert "ZERODHA_TOTP_SECRET" in str(exc.value)

    def test_missing_multiple_lists_all(self):
        env = {"ZERODHA_USER_ID": "AB1234"}
        with pytest.raises(ValueError) as exc:
            read_credentials(env)
        msg = str(exc.value)
        for var in (
            "ZERODHA_PASSWORD",
            "ZERODHA_TOTP_SECRET",
            "BROKER_API_KEY",
            "BROKER_API_SECRET",
        ):
            assert var in msg

    def test_blank_value_counts_as_missing(self):
        env = dict(self.FULL, ZERODHA_PASSWORD="   ")
        with pytest.raises(ValueError) as exc:
            read_credentials(env)
        assert "ZERODHA_PASSWORD" in str(exc.value)

    def test_error_never_contains_values(self):
        # A missing-var error must name variables, never leak the ones present.
        env = {"ZERODHA_PASSWORD": "supersecret", "ZERODHA_USER_ID": "AB1234"}
        with pytest.raises(ValueError) as exc:
            read_credentials(env)
        assert "supersecret" not in str(exc.value)


class TestRedact:
    def test_query_string_password(self):
        out = redact("user_id=AB1234&password=hunter2&x=1")
        assert "hunter2" not in out
        assert "[REDACTED]" in out
        assert "AB1234" in out  # non-secret preserved

    def test_json_access_token(self):
        out = redact('{"access_token": "abc.def.ghi", "status": "ok"}')
        assert "abc.def.ghi" not in out
        assert "ok" in out

    def test_totp_and_request_token(self):
        out = redact("twofa_value=123456&request_token=rt_secret")
        assert "123456" not in out
        assert "rt_secret" not in out

    def test_checksum_and_secret(self):
        out = redact("api_secret=xyz&checksum=deadbeef")
        assert "xyz" not in out
        assert "deadbeef" not in out

    def test_case_insensitive(self):
        out = redact("PASSWORD=Hunter2")
        assert "Hunter2" not in out

    def test_plain_text_unchanged(self):
        assert redact("just a normal message") == "just a normal message"

    def test_empty(self):
        assert redact("") == ""
