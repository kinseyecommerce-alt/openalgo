"""
===============================================================================
                    RSI MEAN-REVERSION INTRADAY STRATEGY
                            OpenAlgo Trading Bot
===============================================================================

Long entry when RSI(14) crosses up through the oversold level; exit on the
overbought cross-down, rupee stop-loss/target, or the intraday cutoff time.
Optional short side mirrors the long logic (TRADE_DIRECTION=SHORT/BOTH).

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    python rsi_meanreversion_strategy.py

Run via OpenAlgo's /python strategy runner:
    OPENALGO_API_KEY            : injected per-strategy.
    OPENALGO_STRATEGY_EXCHANGE  : set from the strategy's exchange config and
                                  drives the host's calendar/holiday gating.
    STRATEGY_ID / STRATEGY_NAME : injected for log/order tagging.
    HOST_SERVER / WEBSOCKET_URL : inherited from OpenAlgo's .env.
    No code changes required. Works unmodified in sandbox (analyzer) mode.

The signal logic (compute_rsi / crossed_above / crossed_below / decide /
check_price_exit) is pure and unit-tested offline in
test/test_rsi_meanreversion_strategy.py.
"""

import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ===============================================================================
# CONFIGURATION (env vars, read once at startup)
# ===============================================================================

API_KEY = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
WS_URL = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")

SYMBOL = os.getenv("SYMBOL", "SBIN")
EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
QUANTITY = int(os.getenv("QUANTITY", "1"))
PRODUCT = os.getenv("PRODUCT", "MIS")
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "5m")

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "70"))

STOPLOSS = float(os.getenv("STOPLOSS", "1.0"))
TARGET = float(os.getenv("TARGET", "2.0"))

# Exit style (project-wide convention: every strategy ships both modes):
#   TRAIL  (default) - once price moves BREAKEVEN_R x STOPLOSS in favor, the
#            stop RATCHETS behind the best price reached at TRAIL_R x STOPLOSS
#            distance, floored at cost, never loosening. The fixed TARGET is
#            disabled so a winner runs for the whole move. RSI-signal exits
#            still apply.
#   TARGET - fixed rupee TARGET exit (the original behavior).
EXIT_MODE = os.getenv("EXIT_MODE", "TRAIL").upper()
BREAKEVEN_R = float(os.getenv("BREAKEVEN_R", "1.5"))
TRAIL_R = float(os.getenv("TRAIL_R", "1.0"))
TRADE_DIRECTION = os.getenv("TRADE_DIRECTION", "LONG").upper()
LOOKBACK_DAYS = max(1, min(30, int(os.getenv("LOOKBACK_DAYS", "5"))))
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "15"))
# Default cutoff per exchange session: MCX trades into the evening, so its
# square-off must sit before the 23:30 exchange deadline, not at 15:10.
_EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "23:25" if _EXCHANGE == "MCX" else "15:10")

STRATEGY_NAME = os.getenv("STRATEGY_NAME", os.getenv("STRATEGY_ID", "RSI_MEANREV"))

# States
FLAT = "FLAT"
LONG = "LONG"
SHORT = "SHORT"

# Actions
ENTER_LONG = "ENTER_LONG"
ENTER_SHORT = "ENTER_SHORT"
EXIT = "EXIT"
HOLD = "HOLD"


def log(message: str) -> None:
    """Plain-text timestamped logging for the /python live log stream."""
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# PURE SIGNAL LOGIC (unit-testable, no network, no SDK)
# ===============================================================================


def compute_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder-smoothed RSI aligned to closes; first `period` entries are nan."""
    n = len(closes)
    rsi = [math.nan] * n
    if n <= period:
        return rsi

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    rsi[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = _rsi(avg_gain, avg_loss)
    return rsi


def crossed_above(prev: float, curr: float, level: float) -> bool:
    """True when the series crossed up through level between prev and curr."""
    if math.isnan(prev) or math.isnan(curr):
        return False
    return prev <= level < curr


def crossed_below(prev: float, curr: float, level: float) -> bool:
    """True when the series crossed down through level between prev and curr."""
    if math.isnan(prev) or math.isnan(curr):
        return False
    return prev >= level > curr


def decide(
    state: str,
    rsi_prev: float,
    rsi_curr: float,
    oversold: float,
    overbought: float,
    direction: str,
    past_cutoff: bool,
) -> str:
    """Full entry/exit state machine, excluding stop-loss/target price checks."""
    if state in (LONG, SHORT) and past_cutoff:
        return EXIT

    if state == LONG:
        if crossed_below(rsi_prev, rsi_curr, overbought):
            return EXIT
        return HOLD
    if state == SHORT:
        if crossed_above(rsi_prev, rsi_curr, oversold):
            return EXIT
        return HOLD

    # FLAT
    if past_cutoff:
        return HOLD
    if crossed_above(rsi_prev, rsi_curr, oversold) and direction in ("LONG", "BOTH"):
        return ENTER_LONG
    if crossed_below(rsi_prev, rsi_curr, overbought) and direction in ("SHORT", "BOTH"):
        return ENTER_SHORT
    return HOLD


def check_price_exit(
    state: str, ltp: float, stoploss_price: float, target_price: float
) -> str | None:
    """Return 'STOPLOSS' / 'TARGET' when the price mandates an exit, else None."""
    if state == LONG:
        if ltp <= stoploss_price:
            return "STOPLOSS"
        if ltp >= target_price:
            return "TARGET"
    elif state == SHORT:
        if ltp >= stoploss_price:
            return "STOPLOSS"
        if ltp <= target_price:
            return "TARGET"
    return None


def update_trailing_stop(
    side: str,
    entry: float,
    initial_risk: float,
    extreme: float,
    current_stop: float,
    breakeven_r: float = 1.5,
    trail_r: float = 1.0,
) -> float:
    """Ratcheting trailing stop -- returns the new stop, never a looser one.

    `extreme` is the best price reached since entry (highest for LONG,
    lowest for SHORT). Until the move reaches breakeven_r x risk the stop is
    untouched (the initial stop protects the trade). From there the stop
    trails the extreme by trail_r x risk, floored at cost, and only ever
    tightens -- a winner keeps running until the trend actually gives back
    the trail distance, capturing the whole move.
    """
    if initial_risk <= 0:
        return current_stop
    move = (extreme - entry) if side == LONG else (entry - extreme)
    if move < breakeven_r * initial_risk:
        return current_stop
    trail_distance = trail_r * initial_risk
    if side == LONG:
        candidate = max(entry, round(extreme - trail_distance, 2))
        return max(current_stop, candidate)
    candidate = min(entry, round(extreme + trail_distance, 2))
    return min(current_stop, candidate)


# ===============================================================================
# I/O SHELL (SDK client, orders, polling loop)
# ===============================================================================


class RSIMeanReversionBot:
    def __init__(self):
        from openalgo import api  # lazy import keeps signal logic import-safe offline

        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.state = FLAT
        self.entry_price = 0.0
        self.stoploss_price = 0.0
        self.target_price = 0.0
        # Explicit risk-armed flag. Never infer "armed" from stoploss_price>0:
        # a SHORT with unknown entry price computes SL = 0 + STOPLOSS > 0 and
        # a numeric gate would fire an instant spurious stop-loss.
        self.armed = False
        self.last_signal_candle = None  # dedupe key: datetime of last acted candle
        # Set when an entry order was accepted but its fill was unresolved and
        # the position book did not show it yet: (side, deadline_epoch). The
        # main loop adopts the position if it appears, so a slow fill cannot
        # cause a doubled entry on the next cycle.
        self.pending_entry: tuple[str, float] | None = None
        # Order id of an exit that was placed but never resolved. Rechecked
        # before any new exit order so sustained orderstatus/positionbook
        # failures cannot stack closing orders (a second fill would flip the
        # account to the opposite side).
        self.pending_exit_order_id: str | None = None
        self.exit_in_progress = False
        self.extreme_price = 0.0  # best price since entry (trailing anchor)
        self.ltp = 0.0
        self.lock = threading.Lock()
        # Serializes the trailing-stop read-compute-write in _risk_check (the
        # WS tick thread and the poll loop both run it). Kept separate from
        # self.lock: exit_position acquires self.lock and threading.Lock is
        # not reentrant, so a shared lock would deadlock on a stop hit.
        self._trail_lock = threading.Lock()
        self.ws_connected = False

    # ------------------------------ helpers ------------------------------

    def past_cutoff(self) -> bool:
        hour, minute = (int(x) for x in SQUARE_OFF_TIME.split(":"))
        now = datetime.now(IST)
        return (now.hour, now.minute) >= (hour, minute)

    def fetch_closed_candles(self):
        """Fetch history and drop the last (partial) candle."""
        end = datetime.now(IST)
        start = end - timedelta(days=LOOKBACK_DAYS)
        df = self.client.history(
            symbol=SYMBOL,
            exchange=EXCHANGE,
            interval=CANDLE_TIMEFRAME,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if df is None or getattr(df, "empty", True) or len(df) < 2:
            return None
        return df.iloc[:-1]  # closed candles only

    def confirm_fill(self, order_id: str) -> tuple[str, float]:
        """Poll orderstatus until the order resolves.

        Returns (outcome, price): outcome is 'filled', 'rejected', or
        'unknown'. Acceptance is NOT a fill -- a rejected order must leave
        state unchanged, so callers only transition on 'filled'.
        """
        for _ in range(5):
            try:
                status = self.client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
                data = status.get("data", {}) if isinstance(status, dict) else {}
                order_status = str(data.get("order_status", "")).lower()
                if order_status == "complete":
                    return "filled", float(data.get("average_price") or 0)
                if order_status in ("rejected", "cancelled"):
                    log(f"Order {order_id} {order_status}")
                    return "rejected", 0.0
            except Exception as e:
                log(f"orderstatus check failed: {e}")
            time.sleep(2)
        return "unknown", 0.0

    def fetch_quote_ltp(self) -> float:
        """REST quote fallback when neither fill price nor WS LTP is available."""
        try:
            quote = self.client.quotes(symbol=SYMBOL, exchange=EXCHANGE)
            data = quote.get("data", {}) if isinstance(quote, dict) else {}
            return float(data.get("ltp") or 0)
        except Exception as e:
            log(f"Quote fetch failed: {e}")
            return 0.0

    def place_market(self, action: str) -> str | None:
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=SYMBOL,
                exchange=EXCHANGE,
                action=action,
                product=PRODUCT,
                quantity=QUANTITY,
                price_type="MARKET",
            )
            if isinstance(response, dict) and response.get("status") == "success":
                order_id = response.get("orderid")
                log(f"{action} order placed: {order_id}")
                return order_id
            log(f"{action} order failed: {response}")
        except Exception as e:
            log(f"{action} order error: {e}")
        return None

    # --------------------------- position logic ---------------------------

    def reconcile_position(self):
        """On startup, adopt an existing open MIS position instead of assuming flat."""
        try:
            book = self.client.positionbook()
            positions = book.get("data", []) if isinstance(book, dict) else []
            for pos in positions:
                if (
                    pos.get("symbol") == SYMBOL
                    and pos.get("exchange") == EXCHANGE
                    and pos.get("product") == PRODUCT
                ):
                    qty = int(float(pos.get("quantity", 0)))
                    if qty == 0:
                        continue
                    price = float(pos.get("average_price") or 0)
                    if price <= 0:
                        # A zero basis would arm SL=-x / target=+y around 0 and
                        # instantly square off the adopted position. Use a live
                        # quote instead; if that also fails, adopt with risk
                        # levels disarmed (cutoff still protects the position).
                        price = self.fetch_quote_ltp()
                        if price <= 0:
                            log("WARNING: adopting position without price - SL/target disarmed")
                    side = LONG if qty > 0 else SHORT
                    self._arm_position(side, price)
                    log(
                        f"Reconciled existing {self.state} position: qty={qty} "
                        f"entry={price:.2f} SL={self.stoploss_price:.2f} "
                        f"target={self.target_price:.2f}"
                    )
                    return
        except Exception as e:
            log(f"Position reconciliation failed (starting FLAT): {e}")
        log("No existing position found - starting FLAT")

    def _risk_levels_for(self, side: str, entry_price: float) -> tuple[float, float]:
        if side == LONG:
            return round(entry_price - STOPLOSS, 2), round(entry_price + TARGET, 2)
        return round(entry_price + STOPLOSS, 2), round(entry_price - TARGET, 2)

    def _arm_position(self, side: str, entry_price: float):
        """Publish a position atomically: risk levels armed BEFORE state.

        The WebSocket LTP callback reads state first, so state must become
        LONG/SHORT only after stoploss/target hold real values -- otherwise a
        tick arriving mid-entry sees SL=0/target=0 and instantly exits the
        brand-new position. With no usable entry price, the position is
        adopted DISARMED (self.armed False) and armed later from the first
        live tick; the cutoff exit protects it meanwhile.
        """
        self.entry_price = entry_price
        # Seed the trailing anchor BEFORE armed becomes True: a concurrent
        # _risk_check must never observe an armed position with extreme=0.
        self.extreme_price = entry_price
        if entry_price > 0:
            self.stoploss_price, self.target_price = self._risk_levels_for(side, entry_price)
            if EXIT_MODE != "TARGET":
                # TRAIL mode: no fixed profit cap -- the ratcheting stop (and
                # the RSI-signal exit) decide when the winner is done.
                self.target_price = math.inf if side == LONG else -math.inf
            self.armed = True
        else:
            self.stoploss_price = self.target_price = 0.0
            self.armed = False
        self.state = side  # publish last

    def enter(self, side: str):
        if self.pending_entry:
            # An earlier accepted-but-unresolved entry order may still fill.
            # Placing another order now could double (or oppose) the position,
            # and the exit path only closes QUANTITY. Wait until the pending
            # window adopts or expires; the crossing is not consumed so a
            # still-valid signal retries afterwards.
            log(f"Skipping {side} entry - pending entry order still unresolved")
            return False
        action = "BUY" if side == LONG else "SELL"
        order_id = self.place_market(action)
        if not order_id:
            return False  # stay FLAT; candle not consumed, fresh signal retries
        outcome, fill_price = self.confirm_fill(order_id)
        if outcome == "rejected":
            return False  # broker refused -- state stays FLAT; crossing retries
        if outcome == "unknown" and self._position_exists(side) is not True:
            # Order accepted but unresolved and not visible in the book yet
            # (slow fill, or the book call itself failed). The order may STILL
            # fill later, so the crossing must be consumed (return True) to
            # prevent a doubled entry next cycle; the main loop adopts the
            # position via pending_entry if it appears within the window.
            self.pending_entry = (side, time.time() + 300)
            log("Entry unresolved - crossing consumed, watching book for late fill")
            return True
        if fill_price <= 0:
            fill_price = self.ltp or self.fetch_quote_ltp()
        if fill_price <= 0:
            log("WARNING: no entry price available - SL/target disarmed until first tick")
        self._arm_position(side, fill_price)
        log(
            f"Entered {side} at {self.entry_price:.2f} "
            f"(SL {self.stoploss_price:.2f}, target {self.target_price:.2f})"
        )
        return True

    def _position_exists(self, side: str) -> bool | None:
        """Tri-state: True/False from the book, None when the check FAILED.

        Callers must treat None as 'unknown', never as 'no position' -- an API
        error while deciding whether an exit completed must fail towards
        'assume still open and retry', not towards abandoning a live position.
        """
        try:
            book = self.client.positionbook()
            positions = book.get("data", []) if isinstance(book, dict) else []
            for pos in positions:
                if (
                    pos.get("symbol") == SYMBOL
                    and pos.get("exchange") == EXCHANGE
                    and pos.get("product") == PRODUCT
                ):
                    qty = int(float(pos.get("quantity", 0)))
                    if (side == LONG and qty > 0) or (side == SHORT and qty < 0):
                        return True
            return False
        except Exception as e:
            log(f"positionbook check failed: {e}")
            return None

    def check_pending_entry(self):
        """Adopt a late-filling entry order once it shows up in the book."""
        if not self.pending_entry or self.state != FLAT:
            return
        side, deadline = self.pending_entry
        exists = self._position_exists(side)
        if exists is True:
            price = self.ltp or self.fetch_quote_ltp()
            self._arm_position(side, price)
            self.pending_entry = None
            log(f"Adopted late-filled {side} entry at {price:.2f}")
        elif time.time() > deadline:
            self.pending_entry = None
            log("Pending entry window expired - no fill appeared, staying FLAT")

    def exit_position(self, reason: str):
        with self.lock:
            if self.state == FLAT or self.exit_in_progress:
                return
            self.exit_in_progress = True
        try:
            side = self.state

            # An earlier exit order may have gone unresolved. Re-check IT
            # before placing another closing order -- if it actually filled,
            # a second exit would flip the account to the opposite side.
            if self.pending_exit_order_id:
                outcome, _ = self.confirm_fill(self.pending_exit_order_id)
                if outcome == "filled" or self._position_exists(side) is False:
                    log(f"Earlier exit order resolved - exited {side} ({reason})")
                    self.pending_exit_order_id = None
                    self._go_flat()
                    return
                if outcome != "rejected":
                    log("Earlier exit order still unresolved - not stacking another")
                    return
                self.pending_exit_order_id = None  # rejected: safe to re-place

            action = "SELL" if side == LONG else "BUY"
            order_id = self.place_market(action)
            if not order_id:
                return  # retry next tick/cycle
            outcome, _ = self.confirm_fill(order_id)
            if outcome == "rejected":
                log(f"Exit order rejected - position still {side}, will retry")
                return
            if outcome == "unknown":
                # Only go FLAT when the book POSITIVELY confirms the position
                # is gone. Both 'still open' (True) and 'check failed' (None)
                # must keep the state and retry -- treating an API error as
                # 'exited' would abandon a live position. Remember the order
                # id so the retry resolves THIS order instead of stacking.
                if self._position_exists(side) is not False:
                    self.pending_exit_order_id = order_id
                    log("Exit unresolved - assuming still open, will retry")
                    return
            log(f"Exited {side} ({reason})")
            self.pending_exit_order_id = None
            self._go_flat()
        finally:
            self.exit_in_progress = False

    def _go_flat(self):
        self.state = FLAT
        self.armed = False
        self.entry_price = self.stoploss_price = self.target_price = 0.0
        self.extreme_price = 0.0

    # ------------------------------ live LTP ------------------------------

    def _on_ltp(self, data):
        try:
            ltp = float(data.get("ltp", 0) or (data.get("data") or {}).get("ltp", 0))
            if ltp > 0:
                self.ltp = ltp
                # Arm a position that was adopted without a known entry price
                # from the first live tick (best available basis).
                if self.state != FLAT and not self.armed:
                    self._arm_position(self.state, ltp)
                    log(f"Armed {self.state} risk levels from first tick at {ltp:.2f}")
                # Only evaluate SL/target when explicitly armed. Never infer
                # armed-ness from stoploss_price>0: a SHORT with entry 0 gets
                # SL = 0 + STOPLOSS > 0 and would instantly stop out.
                self._risk_check(ltp)
        except Exception as e:
            log(f"LTP handler error: {e}")

    def _risk_check(self, price: float):
        """Stop / target / trailing management (WS tick and poll fallback)."""
        if self.state == FLAT or not self.armed or price <= 0:
            return
        reason = check_price_exit(self.state, price, self.stoploss_price, self.target_price)
        if reason:
            self.exit_position(reason)
            return
        if EXIT_MODE == "TARGET":
            return
        # TRAIL mode: track the best price since entry, ratchet the stop.
        # Serialized under _trail_lock: without it, the WS tick thread and the
        # poll loop can interleave the stop read-compute-write and a stale
        # read would overwrite the other thread's tighter ratchet. The stop
        # check and exit_position above deliberately stay OUTSIDE this lock so
        # order placement is never performed while holding it.
        with self._trail_lock:
            if self.state == LONG:
                self.extreme_price = max(self.extreme_price, price)
            else:
                self.extreme_price = (
                    min(self.extreme_price, price) if self.extreme_price else price
                )
            new_stop = update_trailing_stop(
                self.state,
                self.entry_price,
                STOPLOSS,
                self.extreme_price,
                self.stoploss_price,
                BREAKEVEN_R,
                TRAIL_R,
            )
            if new_stop != self.stoploss_price:
                # Log meaningful ratchets only so a fast tape does not spam.
                if abs(new_stop - self.stoploss_price) >= max(0.05, 0.1 * STOPLOSS):
                    log(
                        f"Trailing stop {self.stoploss_price:.2f} -> {new_stop:.2f} "
                        f"(extreme {self.extreme_price:.2f})"
                    )
                self.stoploss_price = new_stop

    def start_ltp_feed(self):
        """Best-effort WebSocket LTP for real-time SL/target; REST fallback if it fails."""

        def _run():
            try:
                self.client.connect()
                self.client.subscribe_ltp(
                    [{"exchange": EXCHANGE, "symbol": SYMBOL}],
                    on_data_received=self._on_ltp,
                )
                self.ws_connected = True
                log("WebSocket LTP feed connected")
            except Exception as e:
                log(f"WebSocket unavailable, using candle-close SL checks: {e}")

        threading.Thread(target=_run, daemon=True, name="LTPFeed").start()

    # ------------------------------ main loop ------------------------------

    def run(self):
        if not API_KEY:
            log("ERROR: OPENALGO_API_KEY not set")
            sys.exit(1)
        if RSI_OVERSOLD >= RSI_OVERBOUGHT:
            log("ERROR: RSI_OVERSOLD must be below RSI_OVERBOUGHT")
            sys.exit(1)
        direction = TRADE_DIRECTION if TRADE_DIRECTION in ("LONG", "SHORT", "BOTH") else "LONG"
        if direction != TRADE_DIRECTION:
            log(f"WARNING: invalid TRADE_DIRECTION {TRADE_DIRECTION!r}, using LONG")

        if EXIT_MODE == "TARGET":
            exit_desc = f"fixed target {TARGET:.2f}"
        else:
            if EXIT_MODE != "TRAIL":
                log(f"WARNING: invalid EXIT_MODE {EXIT_MODE!r}, using TRAIL")
            exit_desc = (
                f"trailing stop {TRAIL_R}x{STOPLOSS:.2f} behind the extreme from "
                f"{BREAKEVEN_R}x risk (no profit cap - winners run)"
            )
        log(
            f"RSI mean-reversion starting: {SYMBOL} {EXCHANGE} {CANDLE_TIMEFRAME} "
            f"RSI({RSI_PERIOD}) {RSI_OVERSOLD}/{RSI_OVERBOUGHT} qty={QUANTITY} "
            f"direction={direction} exit: {exit_desc} cutoff={SQUARE_OFF_TIME} IST"
        )
        self.reconcile_position()
        self.start_ltp_feed()

        while True:
            try:
                past_cutoff = self.past_cutoff()
                candles = self.fetch_closed_candles()
                if candles is None or len(candles) < RSI_PERIOD + 2:
                    log("Insufficient candle data - skipping cycle")
                else:
                    closes = [float(c) for c in candles["close"].tolist()]
                    rsi = compute_rsi(closes, RSI_PERIOD)
                    rsi_prev, rsi_curr = rsi[-2], rsi[-1]
                    candle_key = candles.index[-1]

                    # Entry signals must come from TODAY's candles (IST).
                    # Off-hours and just-after-midnight runs otherwise act on
                    # yesterday's stale pre-close crossings.
                    try:
                        candle_is_fresh = (
                            candle_key.date() == datetime.now(IST).date()
                        )
                    except AttributeError:
                        candle_is_fresh = True  # non-datetime index: don't block

                    # Adopt a late-filling entry order if one is outstanding.
                    self.check_pending_entry()

                    # REST SL/target/trailing check every cycle. Runs even when
                    # the WS feed claims to be up -- a silently-dead feed must
                    # not disable risk enforcement; exit_position dedupes. Uses
                    # a LIVE price (WS LTP, REST quote fallback), never a candle
                    # close: the last close can be a full candle old (and from
                    # BEFORE entry right after a fill), and a stale extreme
                    # would ratchet the trailing stop against a fresh position.
                    live_price = self.ltp or self.fetch_quote_ltp()
                    self._risk_check(live_price)
                    if live_price <= 0 and self.state != FLAT and self.armed:
                        # Degraded mode (WS dead AND quotes failing, history
                        # still up): still enforce the stop against the last
                        # closed candle so the position is not left with only
                        # the cutoff exit. STOP-HIT CHECK ONLY -- the trailing
                        # ratchet stays live-price-only and must never act on
                        # a stale candle close.
                        reason = check_price_exit(
                            self.state, closes[-1], self.stoploss_price, self.target_price
                        )
                        if reason:
                            self.exit_position(reason)

                    action = decide(
                        self.state,
                        rsi_prev,
                        rsi_curr,
                        RSI_OVERSOLD,
                        RSI_OVERBOUGHT,
                        direction,
                        past_cutoff,
                    )
                    if action == EXIT:
                        self.exit_position("cutoff" if past_cutoff else "RSI signal")
                    elif (
                        action in (ENTER_LONG, ENTER_SHORT)
                        and candle_is_fresh
                        and candle_key != self.last_signal_candle
                    ):
                        log(
                            f"Signal {action}: RSI {rsi_prev:.2f} -> {rsi_curr:.2f} "
                            f"on candle {candle_key}"
                        )
                        # Consume the candle only on an accepted entry, so a
                        # transient order failure retries on the next cycle
                        # instead of permanently swallowing the crossing.
                        if self.enter(LONG if action == ENTER_LONG else SHORT):
                            self.last_signal_candle = candle_key
            except Exception as e:
                log(f"Cycle error: {e}")
            time.sleep(SIGNAL_CHECK_INTERVAL)


if __name__ == "__main__":
    RSIMeanReversionBot().run()
