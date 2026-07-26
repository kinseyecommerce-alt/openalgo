"""
===============================================================================
              FOUR-EMA RETRACEMENT INTRADAY STRATEGY (BUY + SELL)
                            OpenAlgo Trading Bot
===============================================================================

Implements the "F&O stocks, 3-minute timeframe" retracement system:

BUY side:
  - Price trading above the previous day's high
  - Price above all 4 EMAs (55, 89, 144, 233), EMAs stacked in sequence
    (EMA55 > EMA89 > EMA144 > EMA233)
  - Retracement: a RED candle touching any of the 4 EMAs = Focus Candle
  - Immediately a GREEN candle closing above the Focus Candle's high
    = Confirmation Candle
  - RSI(14) taking support at 40 (at/above 40 on the confirmation candle)
  - Enter at the next candle open; SL below the Focus Candle low with buffer
  - At 1.5R move SL to cost; at 3R book profits
  - Skip the trade when the risk exceeds 1 percent of the price

SELL side mirrors: below previous day low, EMAs stacked down, GREEN focus /
RED confirmation closing below the focus low, RSI resistance at 60, SL above
the focus high.

Scans a watchlist of symbols and holds at most ONE position at a time.

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    python four_ema_retracement_strategy.py

Run via OpenAlgo's /python strategy runner: env vars are injected by the host
(OPENALGO_API_KEY, OPENALGO_STRATEGY_EXCHANGE, STRATEGY_ID/STRATEGY_NAME,
HOST_SERVER, WEBSOCKET_URL). Works unmodified in sandbox (analyzer) mode.

The signal logic (compute_ema / compute_rsi / emas_stacked / detect_setup /
manage_position / check_stop) is pure and unit-tested offline in
test/test_four_ema_retracement_strategy.py.
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

EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))
# Comma-separated OpenAlgo symbols. The strategy documents target liquid F&O
# stocks; scanning is capped to respect API rate limits.
WATCHLIST = [
    s.strip().upper()
    for s in os.getenv(
        "WATCHLIST", "RELIANCE,HDFCBANK,ICICIBANK,INFY,TCS,SBIN,AXISBANK,LT,ITC,TATAMOTORS"
    ).split(",")
    if s.strip()
]
MAX_SCAN_SYMBOLS = 20
QUANTITY = int(os.getenv("QUANTITY", "1"))
PRODUCT = os.getenv("PRODUCT", "MIS")
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "3m")

EMA_PERIODS = (55, 89, 144, 233)
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_BUY_SUPPORT = float(os.getenv("RSI_BUY_SUPPORT", "40"))
RSI_SELL_RESISTANCE = float(os.getenv("RSI_SELL_RESISTANCE", "60"))

# Stop-loss buffer below/above the focus candle ("few points"), as a fraction
# of price. 0.0005 = 0.05 percent (~0.75 points on a 1500-rupee stock).
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "0.0005"))
# Hard risk cap from the documents: risk must not exceed 1 percent of price.
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.01"))
BREAKEVEN_R = float(os.getenv("BREAKEVEN_R", "1.5"))
TARGET_R = float(os.getenv("TARGET_R", "3.0"))

TRADE_DIRECTION = os.getenv("TRADE_DIRECTION", "BOTH").upper()
LOOKBACK_DAYS = max(2, min(30, int(os.getenv("LOOKBACK_DAYS", "10"))))
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "20"))
ENTRY_CUTOFF_TIME = os.getenv("ENTRY_CUTOFF_TIME", "14:45")
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "23:25" if EXCHANGE == "MCX" else "15:10")

STRATEGY_NAME = os.getenv("STRATEGY_NAME", os.getenv("STRATEGY_ID", "FOUR_EMA_RETRACE"))

FLAT = "FLAT"
LONG = "LONG"
SHORT = "SHORT"


def log(message: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# PURE SIGNAL LOGIC (unit-testable, no network, no SDK)
# ===============================================================================


def compute_ema(values: list[float], period: int) -> list[float]:
    """EMA aligned to values; entries before the SMA seed are nan."""
    n = len(values)
    ema = [math.nan] * n
    if n < period:
        return ema
    seed = sum(values[:period]) / period
    ema[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, n):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder-smoothed RSI aligned to closes; first `period` entries are nan."""
    n = len(closes)
    rsi = [math.nan] * n
    if n <= period:
        return rsi
    gains = losses = 0.0
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
        return 100.0 - (100.0 / (1.0 + g / l))

    rsi[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = _rsi(avg_gain, avg_loss)
    return rsi


def emas_stacked(ema_values: list[float], side: str) -> bool:
    """EMAs 'in sequence': fastest above slowest for LONG, inverted for SHORT.

    ema_values is ordered fastest-to-slowest, e.g. [ema55, ema89, ema144, ema233].
    """
    if any(math.isnan(v) for v in ema_values):
        return False
    if side == LONG:
        return all(ema_values[i] > ema_values[i + 1] for i in range(len(ema_values) - 1))
    return all(ema_values[i] < ema_values[i + 1] for i in range(len(ema_values) - 1))


def is_red(candle: dict) -> bool:
    return candle["close"] < candle["open"]


def is_green(candle: dict) -> bool:
    return candle["close"] > candle["open"]


def touched_any_ema(candle: dict, ema_values: list[float]) -> bool:
    """True when the candle's range overlaps any EMA value."""
    return any(
        not math.isnan(e) and candle["low"] <= e <= candle["high"] for e in ema_values
    )


def detect_setup(
    focus: dict,
    confirm: dict,
    focus_emas: list[float],
    confirm_emas: list[float],
    rsi_confirm: float,
    prev_day_high: float,
    prev_day_low: float,
    side: str,
    sl_buffer_pct: float = SL_BUFFER_PCT,
    max_risk_pct: float = MAX_RISK_PCT,
    rsi_buy_support: float = 40.0,
    rsi_sell_resistance: float = 60.0,
) -> dict | None:
    """Evaluate the focus/confirmation candle pair for an entry setup.

    focus and confirm are consecutive CLOSED candles (dicts with
    open/high/low/close). focus_emas / confirm_emas are the 4 EMA values at
    each candle, fastest first. Returns {"stop": .., "risk": .., "ref": ..}
    (ref = confirmation close, the reference entry price) or None.
    """
    if math.isnan(rsi_confirm):
        return None

    ref = confirm["close"]

    if side == LONG:
        if not (
            ref > prev_day_high
            and all(ref > e for e in confirm_emas)
            and emas_stacked(confirm_emas, LONG)
            and is_red(focus)
            and touched_any_ema(focus, focus_emas)
            and is_green(confirm)
            and confirm["close"] > focus["high"]
            and rsi_confirm >= rsi_buy_support
        ):
            return None
        stop = round(focus["low"] - ref * sl_buffer_pct, 2)
        risk = ref - stop
    else:
        if not (
            ref < prev_day_low
            and all(ref < e for e in confirm_emas)
            and emas_stacked(confirm_emas, SHORT)
            and is_green(focus)
            and touched_any_ema(focus, focus_emas)
            and is_red(confirm)
            and confirm["close"] < focus["low"]
            and rsi_confirm <= rsi_sell_resistance
        ):
            return None
        stop = round(focus["high"] + ref * sl_buffer_pct, 2)
        risk = stop - ref

    if risk <= 0:
        return None
    # Document rule: risking points must not exceed 1 percent of the price.
    if risk > ref * max_risk_pct:
        return None
    return {"side": side, "ref": ref, "stop": stop, "risk": round(risk, 2)}


def manage_position(
    side: str,
    entry: float,
    risk: float,
    ltp: float,
    breakeven_done: bool,
    breakeven_r: float = 1.5,
    target_r: float = 3.0,
) -> str | None:
    """Return 'TARGET' at >= target_r multiples of risk, 'SET_BREAKEVEN' at
    >= breakeven_r multiples (once), else None."""
    if risk <= 0:
        return None
    move = (ltp - entry) if side == LONG else (entry - ltp)
    if move >= target_r * risk:
        return "TARGET"
    if not breakeven_done and move >= breakeven_r * risk:
        return "SET_BREAKEVEN"
    return None


def check_stop(side: str, ltp: float, stop: float) -> bool:
    """True when the stop is hit."""
    if side == LONG:
        return ltp <= stop
    return ltp >= stop


# ===============================================================================
# I/O SHELL (SDK client, scanning, orders, polling loop)
# ===============================================================================


class FourEmaRetracementBot:
    def __init__(self):
        from openalgo import api  # lazy import keeps signal logic import-safe offline

        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.watchlist = WATCHLIST[:MAX_SCAN_SYMBOLS]
        if len(WATCHLIST) > MAX_SCAN_SYMBOLS:
            log(
                f"WARNING: watchlist truncated to {MAX_SCAN_SYMBOLS} symbols "
                f"({len(WATCHLIST) - MAX_SCAN_SYMBOLS} dropped) to respect API limits"
            )

        self.state = FLAT
        self.symbol: str | None = None  # symbol currently held
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.initial_risk = 0.0
        self.breakeven_done = False
        self.armed = False

        self.prev_day_levels: dict[str, tuple[float, float]] = {}  # symbol -> (high, low)
        self.prev_day_date: str | None = None
        self.last_signal_candle: dict[str, object] = {}  # symbol -> candle ts
        self.pending_entry: tuple[str, str, float] | None = None  # (side, symbol, deadline)
        self.pending_exit_order_id: str | None = None
        self.exit_in_progress = False
        self.position_qty = QUANTITY  # actual open quantity (reconcile may differ)
        self.ltp = 0.0
        self.lock = threading.Lock()
        self._ws_started = False
        self._ws_symbol: str | None = None  # symbol currently subscribed on the feed

    # ------------------------------ time gates ------------------------------

    @staticmethod
    def _past(time_str: str) -> bool:
        hour, minute = (int(x) for x in time_str.split(":"))
        now = datetime.now(IST)
        return (now.hour, now.minute) >= (hour, minute)

    def past_cutoff(self) -> bool:
        return self._past(SQUARE_OFF_TIME)

    def past_entry_cutoff(self) -> bool:
        return self._past(ENTRY_CUTOFF_TIME)

    # ------------------------------ market data ------------------------------

    def fetch_closed_candles(self, symbol: str):
        end = datetime.now(IST)
        start = end - timedelta(days=LOOKBACK_DAYS)
        df = self.client.history(
            symbol=symbol,
            exchange=EXCHANGE,
            interval=CANDLE_TIMEFRAME,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if df is None or getattr(df, "empty", True) or len(df) < 2:
            return None
        return df.iloc[:-1]  # closed candles only

    def refresh_prev_day_levels(self):
        """Fetch each symbol's previous trading day high/low.

        Retries only the symbols still missing, every cycle, so one failed
        fetch does not lock a symbol out of scanning for the whole day.
        """
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if self.prev_day_date != today:
            self.prev_day_levels = {}
            self.prev_day_date = today
        missing = [s for s in self.watchlist if s not in self.prev_day_levels]
        if not missing:
            return
        end = datetime.now(IST)
        start = end - timedelta(days=10)
        for symbol in missing:
            try:
                df = self.client.history(
                    symbol=symbol,
                    exchange=EXCHANGE,
                    interval="D",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                )
                if df is None or getattr(df, "empty", True):
                    continue
                # Last row may be today's forming daily candle: use the last
                # row strictly before today as the previous day.
                try:
                    prior = df[df.index.strftime("%Y-%m-%d") < today]
                except (AttributeError, TypeError):
                    # Non-datetime index: drop the last row only when it is
                    # actually today's, else keep the full frame (pre-market
                    # the last row IS the previous day).
                    if str(df.index[-1])[:10] == today and len(df) > 1:
                        prior = df.iloc[:-1]
                    else:
                        prior = df
                if len(prior) == 0:
                    continue
                row = prior.iloc[-1]
                self.prev_day_levels[symbol] = (float(row["high"]), float(row["low"]))
            except Exception as e:
                log(f"{symbol}: previous-day level fetch failed: {e}")
        loaded = len([s for s in self.watchlist if s in self.prev_day_levels])
        newly_loaded = loaded - (len(self.watchlist) - len(missing))
        if newly_loaded > 0:
            log(f"Previous-day levels loaded for {loaded}/{len(self.watchlist)} symbols")

    def fetch_quote_ltp(self, symbol: str) -> float:
        try:
            quote = self.client.quotes(symbol=symbol, exchange=EXCHANGE)
            data = quote.get("data", {}) if isinstance(quote, dict) else {}
            return float(data.get("ltp") or 0)
        except Exception as e:
            log(f"{symbol}: quote fetch failed: {e}")
            return 0.0

    # ------------------------------ orders ------------------------------

    def place_market(self, symbol: str, action: str, quantity: int | None = None) -> str | None:
        try:
            response = self.client.placeorder(
                strategy=STRATEGY_NAME,
                symbol=symbol,
                exchange=EXCHANGE,
                action=action,
                product=PRODUCT,
                quantity=quantity if quantity is not None else QUANTITY,
                price_type="MARKET",
            )
            if isinstance(response, dict) and response.get("status") == "success":
                order_id = response.get("orderid")
                log(f"{symbol}: {action} order placed: {order_id}")
                return order_id
            log(f"{symbol}: {action} order failed: {response}")
        except Exception as e:
            log(f"{symbol}: {action} order error: {e}")
        return None

    def confirm_fill(self, order_id: str) -> tuple[str, float]:
        """Poll orderstatus: ('filled'|'rejected'|'unknown', avg_price)."""
        for _ in range(5):
            try:
                status = self.client.orderstatus(order_id=order_id, strategy=STRATEGY_NAME)
                data = status.get("data", {}) if isinstance(status, dict) else {}
                order_status = str(data.get("order_status", "")).lower()
                if order_status == "complete":
                    return "filled", float(data.get("average_price") or 0)
                if order_status in ("rejected", "cancelled"):
                    return "rejected", 0.0
            except Exception as e:
                log(f"orderstatus check failed: {e}")
            time.sleep(2)
        return "unknown", 0.0

    def _find_position_qty(self, symbol: str, side: str) -> int | None:
        """Signed quantity of the matching open position.

        Tri-state: positive int (open, absolute qty), 0 (positively absent),
        None (check FAILED -- callers must never treat this as absent).
        """
        try:
            book = self.client.positionbook()
            positions = book.get("data", []) if isinstance(book, dict) else []
            for pos in positions:
                if (
                    pos.get("symbol") == symbol
                    and pos.get("exchange") == EXCHANGE
                    and pos.get("product") == PRODUCT
                ):
                    qty = int(float(pos.get("quantity", 0)))
                    if (side == LONG and qty > 0) or (side == SHORT and qty < 0):
                        return abs(qty)
            return 0
        except Exception as e:
            log(f"positionbook check failed: {e}")
            return None

    def _position_exists(self, symbol: str, side: str) -> bool | None:
        qty = self._find_position_qty(symbol, side)
        if qty is None:
            return None
        return qty > 0

    # --------------------------- position lifecycle ---------------------------

    def _arm_position(
        self,
        side: str,
        symbol: str,
        entry_price: float,
        setup_risk: float,
        setup_stop: float,
        qty: int = QUANTITY,
    ):
        """Publish a position atomically: risk levels armed BEFORE state."""
        self.symbol = symbol
        self.position_qty = qty
        self.entry_price = entry_price
        if entry_price > 0:
            # Recompute stop from the ACTUAL fill so slippage cannot widen risk
            # beyond the plan; keep the setup stop when it is tighter.
            if side == LONG:
                self.stop_price = max(setup_stop, round(entry_price - setup_risk, 2))
                self.initial_risk = entry_price - self.stop_price
            else:
                self.stop_price = min(setup_stop, round(entry_price + setup_risk, 2))
                self.initial_risk = self.stop_price - entry_price
            if self.initial_risk > 0:
                self.armed = True
            else:
                # Fill gapped through the setup stop: the plan is already
                # invalidated. Keep the stop ARMED (initial_risk 0 disables
                # breakeven/target math) so the very next price check exits,
                # rather than silently leaving the position unmanaged.
                self.initial_risk = 0.0
                self.armed = True
                log(
                    f"WARNING: {symbol} fill {entry_price:.2f} gapped through "
                    f"stop {self.stop_price:.2f} - exiting on next price check"
                )
        else:
            self.stop_price = 0.0
            self.initial_risk = 0.0
            self.armed = False
            log("WARNING: no entry price available - stop management disarmed")
        self.breakeven_done = False
        self.state = side  # publish last

    def _go_flat(self):
        self.state = FLAT
        self.symbol = None
        self.armed = False
        self.breakeven_done = False
        self.entry_price = self.stop_price = self.initial_risk = 0.0
        self.position_qty = QUANTITY
        self.ltp = 0.0

    def enter(self, side: str, symbol: str, setup: dict) -> bool:
        if self.pending_entry:
            log(f"Skipping {side} {symbol} - pending entry order still unresolved")
            return False
        action = "BUY" if side == LONG else "SELL"
        order_id = self.place_market(symbol, action)
        if not order_id:
            return False
        outcome, fill_price = self.confirm_fill(order_id)
        if outcome == "rejected":
            return False
        if outcome == "unknown":
            exists = self._position_exists(symbol, side)
            if exists is not True:
                # Watch window: the order may still fill. Consume the signal
                # and let check_pending_entry adopt or expire it.
                self.pending_entry = (side, symbol, time.time() + 300)
                log(f"{symbol}: entry unresolved - watching for late fill")
                return True
        if fill_price <= 0:
            # self.ltp may still hold a PREVIOUS symbol's price (the feed for
            # this symbol starts only after arming) -- use a REST quote.
            fill_price = self.fetch_quote_ltp(symbol)
        self._arm_position(side, symbol, fill_price, setup["risk"], setup["stop"])
        self.start_ltp_feed(symbol)
        log(
            f"{symbol}: entered {side} at {self.entry_price:.2f} "
            f"(stop {self.stop_price:.2f}, risk {self.initial_risk:.2f}, "
            f"breakeven at {BREAKEVEN_R}R, target at {TARGET_R}R)"
        )
        return True

    def check_pending_entry(self):
        if not self.pending_entry:
            return
        side, symbol, deadline = self.pending_entry
        qty = self._find_position_qty(symbol, side)
        if qty:
            price = self.fetch_quote_ltp(symbol)
            # Setup stop/risk unknown here; derive risk from the cap.
            risk = round(price * MAX_RISK_PCT, 2) if price > 0 else 0.0
            stop = round(price - risk, 2) if side == LONG else round(price + risk, 2)
            self.pending_entry = None
            self._arm_position(side, symbol, price, risk, stop, qty)
            self.start_ltp_feed(symbol)
            log(f"{symbol}: pending entry adopted as {side} (qty {qty})")
        elif qty == 0 and time.time() > deadline:
            # Expire ONLY on a positive "no position" answer. A failed
            # positionbook check (None) keeps the watch alive -- clearing on
            # an API error while the order actually filled would leave a live
            # position untracked and allow a second entry.
            self.pending_entry = None
            log(f"{symbol}: pending entry expired without a position - cleared")
        elif qty is None:
            log(f"{symbol}: pending entry check failed - keeping watch")

    def exit_position(self, reason: str):
        with self.lock:
            if self.state == FLAT or self.exit_in_progress:
                return
            self.exit_in_progress = True
        try:
            side, symbol = self.state, self.symbol
            if self.pending_exit_order_id:
                outcome, _ = self.confirm_fill(self.pending_exit_order_id)
                if outcome == "filled" or self._position_exists(symbol, side) is False:
                    log(f"{symbol}: earlier exit resolved - exited {side} ({reason})")
                    self.pending_exit_order_id = None
                    self._go_flat()
                    return
                if outcome != "rejected":
                    log("Earlier exit order still unresolved - not stacking another")
                    return
                self.pending_exit_order_id = None
            action = "SELL" if side == LONG else "BUY"
            # Exit the ACTUAL held quantity (reconciled positions may differ
            # from QUANTITY) so no residual position is left unmanaged.
            order_id = self.place_market(symbol, action, self.position_qty)
            if not order_id:
                return
            outcome, _ = self.confirm_fill(order_id)
            if outcome == "rejected":
                log(f"{symbol}: exit order rejected - position still {side}, will retry")
                return
            if outcome == "unknown" and self._position_exists(symbol, side) is not False:
                self.pending_exit_order_id = order_id
                log(f"{symbol}: exit unresolved - assuming still open, will retry")
                return
            log(f"{symbol}: exited {side} ({reason})")
            self.pending_exit_order_id = None
            self._go_flat()
        finally:
            self.exit_in_progress = False

    def reconcile_position(self):
        """Adopt an existing open MIS position for any watchlist symbol."""
        try:
            book = self.client.positionbook()
            positions = book.get("data", []) if isinstance(book, dict) else []
            for pos in positions:
                symbol = pos.get("symbol")
                if (
                    symbol in self.watchlist
                    and pos.get("exchange") == EXCHANGE
                    and pos.get("product") == PRODUCT
                ):
                    qty = int(float(pos.get("quantity", 0)))
                    if qty == 0:
                        continue
                    price = float(pos.get("average_price") or 0) or self.fetch_quote_ltp(symbol)
                    side = LONG if qty > 0 else SHORT
                    risk = round(price * MAX_RISK_PCT, 2) if price > 0 else 0.0
                    stop = round(price - risk, 2) if side == LONG else round(price + risk, 2)
                    # Adopt the position's ACTUAL quantity so a later exit
                    # closes all of it, not just QUANTITY.
                    self._arm_position(side, symbol, price, risk, stop, abs(qty))
                    self.start_ltp_feed(symbol)
                    log(
                        f"Reconciled existing {side} position in {symbol} "
                        f"(qty {abs(qty)}, entry {price:.2f})"
                    )
                    return
        except Exception as e:
            log(f"Position reconciliation failed (starting FLAT): {e}")
        log("No existing position found - starting FLAT")

    # ------------------------------ live LTP ------------------------------

    def _on_ltp(self, data):
        try:
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            tick_symbol = data.get("symbol") or payload.get("symbol")
            # Symbol-scope the tick: after switching positions, late ticks
            # from a previously subscribed symbol must never drive this
            # position's stop/target or entry-price fallback. Fail CLOSED:
            # a tick without a symbol field is dropped too -- if it slipped
            # through after a failed unsubscribe it could belong to the old
            # symbol, and the REST check each cycle covers the gap anyway.
            if tick_symbol != self.symbol:
                return
            ltp = float(payload.get("ltp", 0) or 0)
            if ltp > 0:
                self.ltp = ltp
                self._risk_check(ltp)
        except Exception as e:
            log(f"LTP handler error: {e}")

    def _risk_check(self, price: float):
        """Stop / breakeven / target management. Called from WS and poll loop."""
        if self.state == FLAT or not self.armed or price <= 0:
            return
        if check_stop(self.state, price, self.stop_price):
            self.exit_position("stoploss")
            return
        action = manage_position(
            self.state,
            self.entry_price,
            self.initial_risk,
            price,
            self.breakeven_done,
            BREAKEVEN_R,
            TARGET_R,
        )
        if action == "TARGET":
            self.exit_position(f"target {TARGET_R}R")
        elif action == "SET_BREAKEVEN":
            self.stop_price = self.entry_price
            self.breakeven_done = True
            log(
                f"{self.symbol}: {BREAKEVEN_R}R reached - stop moved to cost "
                f"({self.stop_price:.2f})"
            )

    def start_ltp_feed(self, symbol: str):
        """Subscribe the feed to `symbol`, reusing ONE connection and dropping
        any previous symbol's subscription first (no stacking)."""

        def _run():
            try:
                if not self._ws_started:
                    self.client.connect()
                    self._ws_started = True
                if self._ws_symbol and self._ws_symbol != symbol:
                    try:
                        self.client.unsubscribe_ltp(
                            [{"exchange": EXCHANGE, "symbol": self._ws_symbol}]
                        )
                    except Exception as e:
                        log(f"Unsubscribe {self._ws_symbol} failed (filter still guards): {e}")
                if self._ws_symbol != symbol:
                    self.client.subscribe_ltp(
                        [{"exchange": EXCHANGE, "symbol": symbol}],
                        on_data_received=self._on_ltp,
                    )
                    self._ws_symbol = symbol
                    log(f"{symbol}: WebSocket LTP feed connected")
            except Exception as e:
                log(f"WebSocket unavailable, using candle-close risk checks: {e}")

        threading.Thread(target=_run, daemon=True, name="LTPFeed").start()

    # ------------------------------ scanning ------------------------------

    def scan_symbol(self, symbol: str, direction: str):
        levels = self.prev_day_levels.get(symbol)
        if not levels:
            return
        prev_high, prev_low = levels
        candles = self.fetch_closed_candles(symbol)
        min_candles = max(max(EMA_PERIODS), RSI_PERIOD) + 2
        if candles is None or len(candles) < min_candles:
            return

        closes = [float(c) for c in candles["close"].tolist()]
        emas = {p: compute_ema(closes, p) for p in EMA_PERIODS}
        rsi = compute_rsi(closes, RSI_PERIOD)

        candle_key = candles.index[-1]
        try:
            fresh = candle_key.date() == datetime.now(IST).date()
        except AttributeError:
            # Non-datetime index: fall back to a string-prefix date check so
            # the freshness gate still works instead of silently vanishing.
            fresh = str(candle_key)[:10] == datetime.now(IST).strftime("%Y-%m-%d")
        if not fresh:
            return  # stale candles: off-hours or holiday
        if self.last_signal_candle.get(symbol) == candle_key:
            return

        focus = candles.iloc[-2]
        confirm = candles.iloc[-1]
        focus_c = {k: float(focus[k]) for k in ("open", "high", "low", "close")}
        confirm_c = {k: float(confirm[k]) for k in ("open", "high", "low", "close")}
        focus_emas = [emas[p][-2] for p in EMA_PERIODS]
        confirm_emas = [emas[p][-1] for p in EMA_PERIODS]

        for side in (LONG, SHORT):
            if side == LONG and direction not in ("LONG", "BOTH"):
                continue
            if side == SHORT and direction not in ("SHORT", "BOTH"):
                continue
            setup = detect_setup(
                focus_c,
                confirm_c,
                focus_emas,
                confirm_emas,
                rsi[-1],
                prev_high,
                prev_low,
                side,
                SL_BUFFER_PCT,
                MAX_RISK_PCT,
                RSI_BUY_SUPPORT,
                RSI_SELL_RESISTANCE,
            )
            if setup:
                log(
                    f"{symbol}: {side} setup - confirm close {setup['ref']:.2f}, "
                    f"stop {setup['stop']:.2f}, risk {setup['risk']:.2f} "
                    f"(RSI {rsi[-1]:.1f})"
                )
                if self.enter(side, symbol, setup):
                    self.last_signal_candle[symbol] = candle_key
                return  # one position at a time; stop scanning

    # ------------------------------ main loop ------------------------------

    def run(self):
        if not API_KEY:
            log("ERROR: OPENALGO_API_KEY not set")
            sys.exit(1)
        direction = TRADE_DIRECTION if TRADE_DIRECTION in ("LONG", "SHORT", "BOTH") else "BOTH"
        if direction != TRADE_DIRECTION:
            log(f"WARNING: invalid TRADE_DIRECTION {TRADE_DIRECTION!r}, using BOTH")

        log(
            f"Four-EMA retracement starting: {len(self.watchlist)} symbols on "
            f"{EXCHANGE} {CANDLE_TIMEFRAME}, EMAs {EMA_PERIODS}, "
            f"RSI gate {RSI_BUY_SUPPORT}/{RSI_SELL_RESISTANCE}, "
            f"risk cap {MAX_RISK_PCT * 100:.1f} pct, qty={QUANTITY}, "
            f"direction={direction}, entries until {ENTRY_CUTOFF_TIME}, "
            f"square-off {SQUARE_OFF_TIME} IST"
        )
        self.reconcile_position()

        while True:
            try:
                self.check_pending_entry()
                self.refresh_prev_day_levels()

                if self.state != FLAT:
                    if self.past_cutoff():
                        self.exit_position("intraday cutoff")
                    else:
                        price = self.ltp or self.fetch_quote_ltp(self.symbol)
                        self._risk_check(price)
                elif not self.past_entry_cutoff() and not self.pending_entry:
                    for symbol in self.watchlist:
                        if self.state != FLAT:
                            break
                        self.scan_symbol(symbol, direction)
            except Exception as e:
                log(f"Cycle error: {e}")
            time.sleep(SIGNAL_CHECK_INTERVAL)


if __name__ == "__main__":
    FourEmaRetracementBot().run()
