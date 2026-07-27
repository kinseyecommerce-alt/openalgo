"""
===============================================================================
                    ZERODHA AUTOMATED DAILY LOGIN (TOTP)
                            OpenAlgo Trading Platform
===============================================================================

Automates the once-a-day Zerodha broker login so scheduled strategies can run
hands-free. Indian broker tokens expire daily at ~3 AM IST; this drives Kite's
programmatic login (user id + password + TOTP), exchanges the resulting
request_token for an access_token, and persists it exactly like the manual
web login does -- so the WebSocket feed, order APIs, and the /python host all
pick it up with no further action.

Security model (read this):
  - OpenAlgo is single-user and self-hosted; these are the operator's OWN
    broker credentials on their OWN server. This is the standard, broker-
    sanctioned pattern for unattended algo trading.
  - Credentials come from environment variables ONLY, never hardcoded:
      ZERODHA_USER_ID     - Kite user id (e.g. AB1234)
      ZERODHA_PASSWORD    - Kite login password
      ZERODHA_TOTP_SECRET - the base32 TOTP seed from Kite's external-2FA
                            setup (NOT the 6-digit code; the seed that
                            generates it). Enable "External TOTP" in the Kite
                            profile to obtain this.
      BROKER_API_KEY / BROKER_API_SECRET - already used by the manual login.
  - Every secret (password, TOTP seed/code, tokens, request_token) is REDACTED
    from all log output. Nothing sensitive is ever printed.
  - Store these in .env with tight file permissions (chmod 600). Treat the .env
    as you would the credentials themselves.

Usage:
    # one-off (manual) run:
    uv run python -m broker.zerodha.api.auto_login --user <openalgo_username>

    # scheduled: call run_auto_login(openalgo_username) from a daily job a few
    # minutes after the ~3 AM IST token rollover (e.g. 06:00 IST on trade days).

The pure helpers (generate_totp / extract_request_token / read_credentials /
redact) are network-free and unit-tested in
test/test_zerodha_auto_login.py.
"""

import argparse
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

from utils.logging import get_logger

logger = get_logger(__name__)

# Kite programmatic-login endpoints (the same ones the browser login hits).
KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
KITE_CONNECT_LOGIN_URL = "https://kite.zerodha.com/connect/login"


# ===============================================================================
# PURE HELPERS (no network, no SDK -- unit-tested offline)
# ===============================================================================


def generate_totp(secret: str) -> str:
    """Generate the current 6-digit TOTP code from a base32 seed.

    Args:
        secret: The base32 TOTP seed (spaces are tolerated and stripped).

    Returns:
        The current 6-digit code as a string.

    Raises:
        ValueError: if the secret is empty or not valid base32.
    """
    import pyotp

    if not secret or not secret.strip():
        raise ValueError("TOTP secret is empty")
    cleaned = secret.replace(" ", "").strip()
    try:
        return pyotp.TOTP(cleaned).now()
    except Exception as e:  # pyotp raises binascii.Error on bad base32
        raise ValueError(f"Invalid TOTP secret: {e}") from e


def _is_kite_host(url: str) -> bool:
    """True when the URL's host is a Kite/Zerodha domain (redirect-follow guard)."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host.endswith("zerodha.com") or host.endswith("kite.trade")


def extract_request_token(url: str) -> str | None:
    """Pull the request_token from a Kite post-login redirect URL.

    The connect/login step redirects to the app's registered redirect URL with
    ?request_token=XXXX&action=login&status=success. Returns the token, or None
    if the URL carries no request_token (e.g. an error redirect).
    """
    if not url:
        return None
    try:
        query = parse_qs(urlparse(url).query)
    except Exception:
        return None
    token = query.get("request_token", [None])[0]
    return token or None


# Patterns of sensitive values to scrub from any log line.
_REDACT_KEYS = (
    "password",
    "twofa_value",
    "totp",
    "request_token",
    "access_token",
    "checksum",
    "api_secret",
    "api_key",
    "enctoken",
)


def redact(text: str) -> str:
    """Best-effort scrub of secret-looking key=value / "key":"value" pairs.

    Defence in depth: the flow never intentionally logs secrets, but any string
    passed through here has known-sensitive fields masked so an accidental log
    of a payload or URL cannot leak credentials.
    """
    if not text:
        return text
    out = str(text)
    for key in _REDACT_KEYS:
        # key=value (query string / form)
        out = re.sub(
            rf"({re.escape(key)}=)[^&\s]+", r"\1[REDACTED]", out, flags=re.IGNORECASE
        )
        # "key": "value" (json)
        out = re.sub(
            rf'("{re.escape(key)}"\s*:\s*")[^"]+(")',
            r"\1[REDACTED]\2",
            out,
            flags=re.IGNORECASE,
        )
    return out


def read_credentials(env: dict | None = None) -> dict:
    """Collect and validate the credentials needed for auto-login.

    Args:
        env: mapping to read from (defaults to os.environ). Injectable for tests.

    Returns:
        dict with user_id, password, totp_secret, api_key, api_secret.

    Raises:
        ValueError: listing every missing variable (names only, never values).
    """
    env = os.environ if env is None else env
    fields = {
        "user_id": "ZERODHA_USER_ID",
        "password": "ZERODHA_PASSWORD",
        "totp_secret": "ZERODHA_TOTP_SECRET",
        "api_key": "BROKER_API_KEY",
        "api_secret": "BROKER_API_SECRET",
    }
    creds = {}
    missing = []
    for key, var in fields.items():
        value = (env.get(var) or "").strip()
        if not value:
            missing.append(var)
        creds[key] = value
    if missing:
        raise ValueError(
            "Missing required environment variable(s) for Zerodha auto-login: "
            + ", ".join(missing)
        )
    return creds


# ===============================================================================
# NETWORK FLOW (dedicated short-lived client; secrets never logged)
# ===============================================================================


def fetch_request_token(creds: dict, timeout: float = 30.0) -> str:
    """Drive Kite's programmatic login and return a request_token.

    Steps (the same the browser performs): password login -> TOTP 2FA ->
    connect/login redirect carrying the request_token. Uses a dedicated
    short-lived httpx.Client (own cookie jar, controlled redirects) that is
    always closed via the context manager -- this is not the hot trading path,
    so it does not use the shared pooled client, but it must not leak an FD.

    Raises:
        RuntimeError: on any step failure, with a redacted, actionable message.
    """
    import httpx

    user_id = creds["user_id"]
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        # Step 1: password login -> request_id
        try:
            r1 = client.post(
                KITE_LOGIN_URL, data={"user_id": user_id, "password": creds["password"]}
            )
        except Exception as e:
            raise RuntimeError(f"Kite login request failed: {redact(str(e))}") from e
        if r1.status_code != 200:
            raise RuntimeError(
                f"Kite login rejected (HTTP {r1.status_code}) -- check ZERODHA_USER_ID / "
                f"ZERODHA_PASSWORD. Body: {redact(r1.text)[:200]}"
            )
        try:
            request_id = r1.json()["data"]["request_id"]
        except Exception as e:
            raise RuntimeError(
                f"Kite login response missing request_id: {redact(r1.text)[:200]}"
            ) from e

        # Step 2: TOTP 2FA
        totp_code = generate_totp(creds["totp_secret"])
        try:
            r2 = client.post(
                KITE_TWOFA_URL,
                data={
                    "user_id": user_id,
                    "request_id": request_id,
                    "twofa_value": totp_code,
                    "twofa_type": "totp",
                },
            )
        except Exception as e:
            raise RuntimeError(f"Kite 2FA request failed: {redact(str(e))}") from e
        if r2.status_code != 200:
            raise RuntimeError(
                f"Kite 2FA rejected (HTTP {r2.status_code}) -- check ZERODHA_TOTP_SECRET "
                f"(the base32 seed, not a 6-digit code) and server clock skew. "
                f"Body: {redact(r2.text)[:200]}"
            )

        # Step 3: connect/login redirect carries the request_token.
        try:
            r3 = client.get(
                KITE_CONNECT_LOGIN_URL, params={"api_key": creds["api_key"], "v": "3"}
            )
        except Exception as e:
            raise RuntimeError(f"Kite connect/login request failed: {redact(str(e))}") from e

        # Follow redirects manually so we can read request_token off the Location
        # header the instant it appears (it may be on the app's redirect URL,
        # which we do not want to actually fetch).
        seen = 0
        resp = r3
        while resp.status_code in (301, 302, 303, 307, 308) and seen < 10:
            location = resp.headers.get("location", "")
            token = extract_request_token(location)
            if token:
                return token
            # Only chase further hops that stay on Kite/Zerodha. The token is
            # always read off the Location header above BEFORE any fetch, so a
            # legitimate flow never needs to follow an off-host redirect; refusing
            # to means a manipulated Location cannot make this client issue a
            # request (carrying no cookies cross-domain, but still) to an
            # arbitrary host.
            if not _is_kite_host(location):
                break
            try:
                resp = client.get(location)
            except Exception:
                break
            seen += 1
        # Some flows land the token on the final URL rather than a hop.
        token = extract_request_token(str(resp.url))
        if token:
            return token
        raise RuntimeError(
            "Login succeeded but no request_token was found in the redirect chain. "
            "Confirm the app's redirect URL is registered in the Kite developer "
            "console and that this account is authorized for the API key."
        )


def run_auto_login(openalgo_username: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Perform the full auto-login and persist the token.

    Args:
        openalgo_username: the OpenAlgo account name to store the session under
            (the same name the manual login uses -- typically the admin user).

    Returns:
        (success, message). message is safe to log/display (no secrets).
    """
    try:
        creds = read_credentials()
    except ValueError as e:
        logger.error(str(e))
        return False, str(e)

    logger.info(f"Zerodha auto-login starting for user_id ending {creds['user_id'][-2:]}")
    try:
        request_token = fetch_request_token(creds, timeout=timeout)
    except RuntimeError as e:
        logger.error(f"Auto-login failed: {redact(str(e))}")
        return False, redact(str(e))
    except Exception as e:
        logger.exception("Unexpected error during Zerodha auto-login")
        return False, redact(str(e))

    # Exchange request_token -> access_token via the existing, tested path.
    try:
        from broker.zerodha.api.auth_api import authenticate_broker

        access_token, err = authenticate_broker(request_token)
    except Exception as e:
        logger.exception("Token exchange raised")
        return False, redact(str(e))
    if err or not access_token:
        logger.error(f"Token exchange failed: {redact(str(err or 'no access token'))}")
        return False, redact(str(err or "no access token"))

    # Persist exactly like the manual callback: zerodha stores "api_key:token".
    try:
        from database.auth_db import upsert_auth

        stored = f"{creds['api_key']}:{access_token}"
        upsert_auth(openalgo_username, stored, "zerodha", revoke=False)
    except Exception as e:
        logger.exception("Persisting the auth token failed")
        return False, redact(str(e))

    logger.info("Zerodha auto-login succeeded and token persisted")
    return True, "Zerodha auto-login succeeded"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automated daily Zerodha login (TOTP).")
    parser.add_argument(
        "--user",
        required=True,
        help="OpenAlgo username to store the broker session under (usually the admin user).",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout seconds.")
    args = parser.parse_args(argv)
    ok, message = run_auto_login(args.user, timeout=args.timeout)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
