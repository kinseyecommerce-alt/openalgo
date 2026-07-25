"""
Zerodha WebSocket client backed by the official Kite Connect Python SDK
(pykiteconnect -- kiteconnect.KiteTicker).

Drop-in replacement for the hand-rolled ZerodhaWebSocket client in
zerodha_websocket.py: same constructor signature, same public methods
(start / stop / subscribe_tokens / unsubscribe / set_token_exchange_mapping /
wait_for_connection / is_connected) and the same on_ticks / on_connect /
on_disconnect / on_error callback contract, so ZerodhaWebSocketAdapter can use
either implementation unchanged. Selection happens in zerodha_adapter via the
ZERODHA_WS_CLIENT environment variable.

Threading model: KiteTicker runs on a Twisted reactor. The reactor is
process-wide and NOT restartable, so this module starts it exactly once in a
daemon thread and never calls reactor.stop() -- stop() only closes the ticker's
own connection (stop_retry + close), leaving the reactor alive for other pooled
adapter instances and for reconnects after the daily ~3 AM IST token rollover.
All ticker I/O (subscribe / set_mode / unsubscribe / close) is marshalled onto
the reactor thread via reactor.callFromThread, which is the only thread-safe
entry point Twisted provides.

This client must only run in a non-eventlet process (the dev server, or the
out-of-process WebSocket proxy under gunicorn+eventlet). zerodha_adapter guards
against selecting it inside an eventlet-monkeypatched process.
"""

import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime

from kiteconnect import KiteTicker
from twisted.internet import reactor

from database.auth_db import get_auth_token
from utils.logging import get_logger

if "eventlet" in sys.modules:
    import eventlet

    _real_threading = eventlet.patcher.original("threading")
else:
    _real_threading = threading

# The Twisted reactor is a process-wide singleton and cannot be restarted once
# stopped. Start it at most once, in a daemon thread, and route every later
# connect through callFromThread (safe to call even before the reactor starts:
# the call is queued and runs as soon as the loop is up).
_reactor_lock = _real_threading.Lock()
_reactor_thread_started = False


def _connect_ticker(kws: KiteTicker) -> None:
    """Connect a KiteTicker, starting the shared reactor thread on first use."""
    global _reactor_thread_started
    with _reactor_lock:
        if _reactor_thread_started or reactor.running:
            # KiteTicker.connect() only sets up the client factory and calls
            # connectWS when the reactor is already running; run that on the
            # reactor thread where socket setup is thread-safe.
            reactor.callFromThread(kws.connect, True)
        else:
            # First connection in this process: connect() spawns the daemon
            # reactor thread itself (threaded=True).
            kws.connect(threaded=True)
            _reactor_thread_started = True


class ZerodhaKiteTickerClient:
    """
    Zerodha market data client using the official pykiteconnect KiteTicker.

    Interface-compatible with ZerodhaWebSocket (zerodha_websocket.py).
    """

    # Subscription modes -- identical strings to KiteTicker.MODE_* and to the
    # native client's constants, so adapter-level mode mapping works with both.
    MODE_LTP = KiteTicker.MODE_LTP
    MODE_QUOTE = KiteTicker.MODE_QUOTE
    MODE_FULL = KiteTicker.MODE_FULL

    MAX_TOKENS_PER_SUBSCRIBE = 200
    MAX_INSTRUMENTS_PER_CONNECTION = 3000

    RECONNECT_MAX_DELAY = 60
    RECONNECT_MAX_TRIES = 50

    def __init__(
        self,
        api_key: str,
        access_token: str,
        on_ticks: Callable[[list[dict]], None] = None,
        user_id: str | None = None,
    ):
        self.api_key = api_key
        self.access_token = access_token
        self.on_ticks = on_ticks
        # user_id lets the auth-failure path re-read a fresh access token from
        # the database (tokens roll over daily at ~3 AM IST).
        self.user_id = user_id

        self.logger = get_logger(__name__)
        self.lock = _real_threading.Lock()

        self.kws: KiteTicker | None = None
        self.connected = False
        self.running = False
        self._connection_ready = _real_threading.Event()

        # Wrapper-level subscription state, used to reseed a rebuilt ticker
        # after a token refresh (a fresh KiteTicker starts with no
        # subscriptions and its automatic resubscribe only covers its own
        # in-place reconnects).
        self._token_modes: dict[int, str] = {}
        self.token_exchange_map: dict[int, str] = {}

        # Adapter-facing callbacks (same contract as ZerodhaWebSocket).
        self.on_connect: Callable | None = None
        self.on_disconnect: Callable | None = None
        self.on_error: Callable | None = None

        # Auth/token failure handling, mirroring the native client (#1419):
        # on a 403 do not die immediately -- re-read a fresh token from the DB
        # (bypassing the possibly-stale auth cache of a separate WS-proxy
        # process) and rebuild the ticker a bounded number of times.
        self._auth_refresh_retries = 0
        self._max_auth_refresh_retries = 3
        self._auth_refresh_in_progress = False

        # Statistics (parity with the native client)
        self.tick_count = 0
        self.error_count = 0

        self.logger.info("Zerodha KiteTicker client initialized (official pykiteconnect)")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Build the KiteTicker and connect (reactor runs in a daemon thread)."""
        if self.running:
            self.logger.debug("KiteTicker client already running")
            return True
        try:
            self.running = True
            self._connection_ready.clear()
            self._auth_refresh_retries = 0
            self._build_and_connect()
            self.logger.info("Zerodha KiteTicker client started")
            return True
        except Exception as e:
            self.logger.exception(f"Error starting KiteTicker client: {e}")
            self.running = False
            return False

    def _build_and_connect(self) -> None:
        kws = KiteTicker(
            self.api_key,
            self.access_token,
            reconnect=True,
            reconnect_max_tries=self.RECONNECT_MAX_TRIES,
            reconnect_max_delay=self.RECONNECT_MAX_DELAY,
        )
        kws.on_ticks = self._on_kt_ticks
        kws.on_connect = self._on_kt_connect
        kws.on_close = self._on_kt_close
        kws.on_error = self._on_kt_error
        kws.on_reconnect = self._on_kt_reconnect
        kws.on_noreconnect = self._on_kt_noreconnect
        with self.lock:
            self.kws = kws
        _connect_ticker(kws)

    def stop(self):
        """Close this ticker's connection. Never stops the shared reactor."""
        try:
            self.logger.debug("Stopping KiteTicker client...")
            self.running = False
            self.connected = False
            with self.lock:
                kws = self.kws
                self.kws = None
            if kws is not None:
                try:
                    # close() = stop_retry() + sendClose(); marshal onto the
                    # reactor thread. reactor.stop() is deliberately never
                    # called: the reactor is process-wide, unrestartable, and
                    # may be serving other pooled adapter instances.
                    reactor.callFromThread(kws.close)
                except Exception as e:
                    self.logger.debug(f"Error closing KiteTicker: {e}")
            self.logger.debug("KiteTicker client stopped")
        except Exception as e:
            self.logger.exception(f"Error stopping KiteTicker client: {e}")

    def wait_for_connection(self, timeout: float = 15.0) -> bool:
        """Wait for the WebSocket connection to be established."""
        return self._connection_ready.wait(timeout=timeout)

    def is_connected(self) -> bool:
        """Check if the WebSocket is connected."""
        return self.connected and self.running

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def set_token_exchange_mapping(self, token_exchange_map: dict[int, str]):
        """Set the token to exchange mapping (annotates ticks with source_exchange)."""
        with self.lock:
            self.token_exchange_map.update(token_exchange_map)
        self.logger.debug(f"Updated token exchange mapping for {len(token_exchange_map)} tokens")

    def subscribe_tokens(self, tokens: list[int], mode: str = MODE_QUOTE):
        """Subscribe to tokens in the given mode (batched)."""
        if not self.running:
            self.logger.error("KiteTicker client not running. Call start() first.")
            return
        if not tokens:
            return
        try:
            tokens = [int(token) for token in tokens]
        except (ValueError, TypeError) as e:
            self.logger.error(f"Invalid token format: {e}")
            return

        with self.lock:
            total_after = len(self._token_modes | dict.fromkeys(tokens))
            if total_after > self.MAX_INSTRUMENTS_PER_CONNECTION:
                self.logger.error(
                    f"Cannot subscribe to {len(tokens)} tokens. Would exceed limit "
                    f"of {self.MAX_INSTRUMENTS_PER_CONNECTION}."
                )
                return
            for token in tokens:
                self._token_modes[token] = mode
            kws = self.kws

        if not (self.connected and kws is not None):
            # Not connected yet: _on_kt_connect reseeds every tracked token
            # once the socket is up, so nothing more to do here.
            self.logger.debug(
                f"Not connected -- queued {len(tokens)} tokens for subscribe on connect"
            )
            return

        self._send_subscribe(kws, tokens, mode)

    def _send_subscribe(self, kws: KiteTicker, tokens: list[int], mode: str) -> None:
        """Send subscribe + set_mode for tokens in batches on the reactor thread."""
        for i in range(0, len(tokens), self.MAX_TOKENS_PER_SUBSCRIBE):
            batch = tokens[i : i + self.MAX_TOKENS_PER_SUBSCRIBE]
            try:
                reactor.callFromThread(kws.subscribe, batch)
                reactor.callFromThread(kws.set_mode, mode, batch)
                self.logger.debug(f"Subscribed batch of {len(batch)} tokens in {mode} mode")
            except Exception as e:
                self.logger.error(f"Batch subscription failed: {e}")

    def unsubscribe(self, tokens: list[int]) -> bool:
        """Unsubscribe from tokens."""
        try:
            tokens = [int(token) for token in tokens]
            with self.lock:
                for token in tokens:
                    self._token_modes.pop(token, None)
                    self.token_exchange_map.pop(token, None)
                kws = self.kws
            if self.connected and kws is not None:
                reactor.callFromThread(kws.unsubscribe, tokens)
            self.logger.debug(f"Unsubscribed from {len(tokens)} tokens")
            return True
        except Exception as e:
            self.logger.error(f"Error unsubscribing: {e}")
            return False

    # ------------------------------------------------------------------
    # KiteTicker callbacks (all fire on the reactor thread)
    # ------------------------------------------------------------------

    def _on_kt_connect(self, ws, response):
        self.connected = True
        self._auth_refresh_retries = 0
        self._connection_ready.set()
        self.logger.info("Zerodha KiteTicker connected")

        # Reseed subscriptions on a ticker that has none of its own -- i.e.
        # the first connect, or a rebuilt ticker after a token refresh.
        # In-place reconnects of the same ticker are resubscribed internally
        # by KiteTicker and skip this branch.
        with self.lock:
            kws = self.kws
            needs_seed = kws is not None and not kws.subscribed_tokens and self._token_modes
            modes: dict[str, list[int]] = {}
            if needs_seed:
                for token, mode in self._token_modes.items():
                    modes.setdefault(mode, []).append(token)
        if needs_seed:
            for mode, tokens in modes.items():
                self.logger.info(f"Seeding {len(tokens)} tokens in {mode} mode after connect")
                self._send_subscribe(kws, tokens, mode)

        if self.on_connect:
            try:
                self.on_connect()
            except Exception as e:
                self.logger.error(f"Error in on_connect callback: {e}")

    def _on_kt_close(self, ws, code, reason):
        self.logger.info(f"KiteTicker closed (code={code}, msg={reason})")
        self.connected = False
        if self._is_fatal_auth_error(code, reason):
            self._handle_auth_failure(f"close code={code} reason={reason!r}")
        if self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception as e:
                self.logger.error(f"Error in on_disconnect callback: {e}")

    def _on_kt_error(self, ws, code, reason):
        self.logger.error(f"KiteTicker error (code={code}, msg={reason})")
        self.error_count += 1
        if self._is_fatal_auth_error(code, reason):
            self._handle_auth_failure(f"error code={code} reason={reason!r}")
        if self.on_error:
            try:
                self.on_error(reason)
            except Exception:
                pass

    def _on_kt_reconnect(self, ws, attempts_count):
        self.logger.info(f"KiteTicker reconnecting (attempt {attempts_count})...")

    def _on_kt_noreconnect(self, ws):
        self.logger.error("KiteTicker gave up reconnecting (max retries reached)")
        self.connected = False
        self.running = False

    def _on_kt_ticks(self, ws, ticks):
        if not ticks or not self.on_ticks:
            return
        try:
            normalized = [self._normalise_tick(t) for t in ticks]
            self.tick_count += len(normalized)
            self.on_ticks(normalized)
        except Exception as e:
            self.logger.error(f"Error in on_ticks callback: {e}")
            self.error_count += 1

    # ------------------------------------------------------------------
    # Tick normalisation
    # ------------------------------------------------------------------

    def _normalise_tick(self, tick: dict) -> dict:
        """Normalise a KiteTicker tick to the shape the adapter expects.

        The adapter's _transform_* methods were written against the native
        client's tick dicts, so add the aliases it reads and convert datetime
        values to epoch integers (ticks are JSON-serialised onto the ZMQ bus,
        which datetime objects would break).
        """
        t = dict(tick)
        t["timestamp"] = int(time.time() * 1000)

        last_price = t.get("last_price", 0)
        t["last_traded_price"] = last_price
        if "volume_traded" in t:
            t["volume"] = t["volume_traded"]
        if "average_traded_price" in t:
            t["average_price"] = t["average_traded_price"]

        ohlc = t.get("ohlc")
        if ohlc:
            t["open_price"] = ohlc.get("open", 0)
            t["high_price"] = ohlc.get("high", 0)
            t["low_price"] = ohlc.get("low", 0)
            t["close_price"] = ohlc.get("close", 0)

        # KiteTicker's "change" is percent change from close.
        if "change" in t:
            t["price_change_percent"] = t["change"]
            if ohlc and ohlc.get("close"):
                t["price_change"] = last_price - ohlc["close"]

        if "oi" in t:
            t["open_interest"] = t["oi"]

        # Epoch seconds, matching the native client's raw packet values.
        for key in ("exchange_timestamp", "last_trade_time"):
            value = t.get(key)
            if isinstance(value, datetime):
                t[key] = int(value.timestamp())
            elif value is None:
                t.pop(key, None)

        token = t.get("instrument_token")
        with self.lock:
            exchange = self.token_exchange_map.get(token)
        if exchange:
            t["source_exchange"] = exchange
        return t

    # ------------------------------------------------------------------
    # Auth failure / daily token rollover handling
    # ------------------------------------------------------------------

    _AUTH_FAILURE_INDICATORS = (
        "403",
        "forbidden",
        "401",
        "unauthorized",
        "tokenexception",
        "invalid api_key",
        "invalid access_token",
        "invalid `api_key`",
        "invalid `access_token`",
        "api_key or access_token",
    )

    def _is_fatal_auth_error(self, code, reason) -> bool:
        """Return True iff the error/close payload looks like an auth failure."""
        if code in (401, 403):
            return True
        if reason is None:
            return False
        text = str(reason).lower()
        return any(token in text for token in self._AUTH_FAILURE_INDICATORS)

    def _handle_auth_failure(self, detail: str) -> None:
        """Refresh the access token from the DB and rebuild the ticker.

        Runs the DB read and rebuild on a real thread so the reactor thread is
        never blocked on database I/O. Bounded retries, and gives up early when
        the DB still holds the same dead token (needs a fresh login).
        """
        with self.lock:
            if self._auth_refresh_in_progress or not self.running:
                return
            self._auth_refresh_in_progress = True

        _real_threading.Thread(
            target=self._auth_refresh_worker, args=(detail,), daemon=True, name="ZerodhaKTAuth"
        ).start()

    def _auth_refresh_worker(self, detail: str) -> None:
        try:
            if self._auth_refresh_retries >= self._max_auth_refresh_retries:
                self.logger.error(
                    f"Stopping KiteTicker -- auth/token failure persisted after "
                    f"{self._max_auth_refresh_retries} token refreshes. Detail: {detail}"
                )
                self.stop()
                return
            self._auth_refresh_retries += 1

            if not self._refresh_access_token():
                self.logger.error(
                    f"Stopping KiteTicker -- auth/token failure and DB token unchanged; "
                    f"needs re-login. Detail: {detail}"
                )
                self.stop()
                return

            self.logger.info(
                f"Auth/token failure -- refreshed token from DB, rebuilding ticker "
                f"(attempt {self._auth_refresh_retries}/{self._max_auth_refresh_retries})"
            )
            # Tear down the old ticker's connection and retry loop, then build
            # a fresh one with the new token baked into its socket URL.
            with self.lock:
                old = self.kws
                self.kws = None
            if old is not None:
                try:
                    reactor.callFromThread(old.close)
                except Exception as e:
                    self.logger.debug(f"Error closing stale ticker: {e}")
            self._connection_ready.clear()
            self._build_and_connect()
        except Exception as e:
            self.logger.exception(f"Error during auth refresh: {e}")
        finally:
            with self.lock:
                self._auth_refresh_in_progress = False

    def _refresh_access_token(self) -> bool:
        """Re-read a fresh access token from the database.

        Returns True if the token changed (worth rebuilding the connection),
        False otherwise. Same semantics as the native client's refresh.
        """
        if not self.user_id:
            return False
        try:
            auth_token = get_auth_token(self.user_id, bypass_cache=True)
            if not auth_token:
                self.logger.warning("No fresh auth token found -- keeping existing token")
                return False
            # Auth token format is api_key:access_token; use the access token part.
            if ":" in auth_token:
                parts = auth_token.split(":")
                access_token = parts[1] if len(parts) >= 2 else auth_token
            else:
                access_token = auth_token
            if not access_token:
                self.logger.warning("Parsed empty access token -- keeping existing token")
                return False
            with self.lock:
                changed = access_token != self.access_token
                self.access_token = access_token
            if changed:
                self.logger.info("Refreshed Zerodha access token from database")
            return changed
        except Exception as e:
            self.logger.exception(f"Error refreshing access token: {e}")
            return False
