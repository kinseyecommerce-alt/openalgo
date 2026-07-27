"""
===============================================================================
                  MULTI-VARIANT INTRADAY STRATEGY ENGINE
                            OpenAlgo Trading Bot
===============================================================================

One engine file hosting TWENTY intraday entry variants behind the exact
order-safety shell of the validated four_ema_retracement_strategy:

  - watchlist scanning with a single position across the watchlist
  - confirm_fill tri-state, pending_entry watch window, pending_exit_order_id
    recheck, _arm_position ordering (extreme seeded before armed, state
    published last), _trail_lock around the trailing ratchet
  - symbol-scoped LTP feed with unsubscribe-before-resubscribe
  - degraded-mode stop fallback against the last closed candle
  - trailing-stop convention (EXIT_MODE TRAIL default / TARGET legacy)
  - NSE/MCX-aware cutoffs, candle freshness gate, per-symbol dedupe

Layers:
  (a) a PURE indicator library (plain lists in/out, nan-padded warmups,
      no pandas): compute_ema, compute_rsi, compute_vwap, compute_atr,
      compute_supertrend, compute_bollinger, compute_macd, compute_stochastic,
      compute_donchian, compute_roc, heikin_ashi, classic_pivots
  (b) a VARIANTS registry: variant_key -> {name, description, entry}. Every
      entry callable has the uniform signature
          entry(candles, prev_day_high, prev_day_low, side) -> dict | None
      where candles are CLOSED candles (dicts with open/high/low/close/volume
      and "ts"), evaluated on the LAST closed candle. A shared wrapper
      enforces the SL buffer on structure stops, positive risk, and the
      1 percent risk cap (MAX_RISK_PCT of the reference price).
  (c) the I/O shell copied from the reference, with detect_setup replaced by
      the active variant's entry() for the allowed sides
  (d) variant resolution from the deployed filename: the copy is named
      "<variant_key>_<YYYYmmddHHMMSS>.py". Fallbacks: VARIANT env, then the
      default "ema_ribbon_trend" with a loud warning.
  (e) MCX support: EXCHANGE env as in the reference. MCX symbols carry
      expiries, so when EXCHANGE=MCX and WATCHLIST is unset the engine scans
      NOTHING (with a clear warning) instead of trading wrong symbols.

Run standalone:
    export OPENALGO_API_KEY="your-api-key"
    export VARIANT="vwap_breakout"      # only needed when the filename
    python variant_intraday_strategy.py # does not carry the variant key

Run via OpenAlgo's /python strategy runner: env vars are injected by the host
(OPENALGO_API_KEY, OPENALGO_STRATEGY_EXCHANGE, STRATEGY_ID/STRATEGY_NAME,
HOST_SERVER, WEBSOCKET_URL). Works unmodified in sandbox (analyzer) mode.

The indicator library, every variant entry, and the position-management
helpers are pure (no network, no SDK) and unit-testable offline.
"""

import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Make the shared watchlist loader importable whether this file runs from
# strategies/scripts, strategies/examples, or a deployed copy in strategies/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watchlist_loader import empty_watchlist_warning, load_watchlist  # noqa: E402

# ===============================================================================
# CONFIGURATION (env vars, read once at startup)
# ===============================================================================

API_KEY = os.getenv("OPENALGO_API_KEY", "")
API_HOST = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
WS_URL = os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765")

EXCHANGE = os.getenv("OPENALGO_STRATEGY_EXCHANGE", os.getenv("EXCHANGE", "NSE"))

# Comma-separated OpenAlgo symbols. The NSE default targets liquid F&O
# stocks; scanning is capped to respect API rate limits. MCX symbols carry
# expiries (e.g. CRUDEOILM20MAY24FUT) so there is NO safe hardcoded MCX
# watchlist: with EXCHANGE=MCX and WATCHLIST unset the engine scans nothing.
# Fallback NSE universe used only when WATCHLIST is unset and no screened
# strategies/watchlists/NSE.txt exists yet. Sized to MAX_SCAN_SYMBOLS (20) so
# the whole scan budget is used with no truncation - these are the most liquid
# large-caps (pure equity, no expiries). Once the pre-market screener runs it
# writes a ranked NSE.txt that takes precedence over this list.
DEFAULT_NSE_WATCHLIST = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "SBIN",
    "AXISBANK",
    "LT",
    "ITC",
    "TATAMOTORS",
    "BHARTIARTL",
    "KOTAKBANK",
    "HINDUNILVR",
    "BAJFINANCE",
    "MARUTI",
    "SUNPHARMA",
    "HCLTECH",
    "TITAN",
    "NTPC",
    "TATASTEEL",
]
_WATCHLIST_ENV = os.getenv("WATCHLIST")
# Shared loader precedence: WATCHLIST env wins (empty string -> no symbols);
# else a screened strategies/watchlists/<EXCHANGE>.txt; else the NSE default
# for NSE and NOTHING for MCX/other expiry-bearing exchanges. This preserves
# the previous behavior exactly (env set -> parse; MCX + unset -> []).
WATCHLIST = load_watchlist(EXCHANGE, DEFAULT_NSE_WATCHLIST, _WATCHLIST_ENV)
MAX_SCAN_SYMBOLS = 20
QUANTITY = int(os.getenv("QUANTITY", "1"))
PRODUCT = os.getenv("PRODUCT", "MIS")
CANDLE_TIMEFRAME = os.getenv("CANDLE_TIMEFRAME", "3m")

# Stop-loss buffer beyond a structure stop ("few points"), as a fraction of
# price. 0.0005 = 0.05 percent (~0.75 points on a 1500-rupee stock).
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "0.0005"))
# Hard risk cap: risk must not exceed 1 percent of the reference price.
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.01"))
BREAKEVEN_R = float(os.getenv("BREAKEVEN_R", "1.5"))
TARGET_R = float(os.getenv("TARGET_R", "3.0"))

# Exit style:
#   TRAIL  (default) - after the breakeven point the stop RATCHETS behind the
#            best price reached (trail distance = TRAIL_R x initial risk),
#            letting a winner run for the whole move instead of capping it.
#   TARGET - the legacy playbook exit: book profits at TARGET_R x risk.
EXIT_MODE = os.getenv("EXIT_MODE", "TRAIL").upper()
TRAIL_R = float(os.getenv("TRAIL_R", "1.0"))

TRADE_DIRECTION = os.getenv("TRADE_DIRECTION", "BOTH").upper()
LOOKBACK_DAYS = max(2, min(30, int(os.getenv("LOOKBACK_DAYS", "10"))))
SIGNAL_CHECK_INTERVAL = int(os.getenv("SIGNAL_CHECK_INTERVAL", "20"))
ENTRY_CUTOFF_TIME = os.getenv("ENTRY_CUTOFF_TIME", "22:45" if EXCHANGE == "MCX" else "14:45")
SQUARE_OFF_TIME = os.getenv("SQUARE_OFF_TIME", "23:25" if EXCHANGE == "MCX" else "15:10")

STRATEGY_NAME = os.getenv("STRATEGY_NAME", os.getenv("STRATEGY_ID", "VARIANT_INTRADAY"))

# Variant tunables shared across variants.
ORB_WINDOW_MINUTES = 15
GAP_MIN_PCT = 0.005  # gap_go: opening gap must exceed 0.5 percent

# Candles handed to a variant per scan (covers EMA200 warmup + full session).
SCAN_MAX_BARS = 400

FLAT = "FLAT"
LONG = "LONG"
SHORT = "SHORT"


def log(message: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ===============================================================================
# (a) PURE INDICATOR LIBRARY (plain lists in/out, nan warmups, no pandas)
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

    def _rsi(g: float, loss: float) -> float:
        if loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + g / loss))

    rsi[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = _rsi(avg_gain, avg_loss)
    return rsi


def compute_sma(values: list[float], period: int) -> list[float]:
    """Simple moving average; nan while warming up or when the window has nan."""
    n = len(values)
    out = [math.nan] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(math.isnan(v) for v in window):
            continue
        out[i] = sum(window) / period
    return out


def compute_rolling_std(values: list[float], period: int) -> list[float]:
    """Rolling population standard deviation; nan warmup / nan-poisoned windows."""
    n = len(values)
    out = [math.nan] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        if any(math.isnan(v) for v in window):
            continue
        mean = sum(window) / period
        out[i] = math.sqrt(sum((v - mean) ** 2 for v in window) / period)
    return out


def _ts_date(ts):
    """Calendar-date key of a candle timestamp (datetime-like or string)."""
    try:
        return ts.date()
    except AttributeError:
        return str(ts)[:10]


def _minutes_between(ts0, ts1) -> float | None:
    """Minutes from ts0 to ts1, or None when the timestamps are not datetimes."""
    try:
        return (ts1 - ts0).total_seconds() / 60.0
    except (TypeError, AttributeError):
        return None


def compute_vwap(candles: list[dict]) -> list[float]:
    """Session-cumulative VWAP, resetting at each candle date boundary.

    candles are dicts with high/low/close/volume and "ts". Uses the typical
    price (H+L+C)/3. nan until the session has traded volume.
    """
    out = []
    cum_pv = cum_v = 0.0
    current_date = None
    for c in candles:
        d = _ts_date(c["ts"])
        if d != current_date:
            cum_pv = cum_v = 0.0
            current_date = d
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = float(c.get("volume", 0) or 0)
        cum_pv += tp * vol
        cum_v += vol
        out.append(cum_pv / cum_v if cum_v > 0 else math.nan)
    return out


def compute_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float]:
    """Wilder-smoothed ATR; first period-1 entries are nan."""
    n = len(closes)
    atr = [math.nan] * n
    if n < period or period <= 0:
        return atr
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def compute_supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 10,
    mult: float = 3.0,
) -> tuple[list[float], list[int]]:
    """Classic Supertrend. Returns (line, direction).

    direction is +1 in an uptrend (line below price), -1 in a downtrend
    (line above price), 0 while warming up. line is nan while warming up.
    """
    n = len(closes)
    line = [math.nan] * n
    direction = [0] * n
    atr = compute_atr(highs, lows, closes, period)
    prev_fub = prev_flb = math.nan
    for i in range(n):
        if math.isnan(atr[i]):
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        ub = hl2 + mult * atr[i]
        lb = hl2 - mult * atr[i]
        if math.isnan(prev_fub):
            fub, flb = ub, lb
            direction[i] = 1 if closes[i] >= hl2 else -1
        else:
            fub = ub if (ub < prev_fub or closes[i - 1] > prev_fub) else prev_fub
            flb = lb if (lb > prev_flb or closes[i - 1] < prev_flb) else prev_flb
            if direction[i - 1] == 1:
                direction[i] = -1 if closes[i] < flb else 1
            else:
                direction[i] = 1 if closes[i] > fub else -1
        line[i] = flb if direction[i] == 1 else fub
        prev_fub, prev_flb = fub, flb
    return line, direction


def compute_bollinger(
    closes: list[float], period: int = 20, k: float = 2.0
) -> tuple[list[float], list[float], list[float]]:
    """Bollinger Bands (population stdev). Returns (middle, upper, lower)."""
    n = len(closes)
    mid = [math.nan] * n
    upper = [math.nan] * n
    lower = [math.nan] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        sd = math.sqrt(sum((v - mean) ** 2 for v in window) / period)
        mid[i] = mean
        upper[i] = mean + k * sd
        lower[i] = mean - k * sd
    return mid, upper, lower


def compute_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    """MACD. Returns (macd_line, signal_line, histogram), nan warmups."""
    n = len(closes)
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    macd = [
        ema_fast[i] - ema_slow[i]
        if not (math.isnan(ema_fast[i]) or math.isnan(ema_slow[i]))
        else math.nan
        for i in range(n)
    ]
    pad = slow - 1
    if n <= pad:
        return macd, [math.nan] * n, [math.nan] * n
    signal_line = [math.nan] * pad + compute_ema(macd[pad:], signal)
    hist = [
        m - s if not (math.isnan(m) or math.isnan(s)) else math.nan
        for m, s in zip(macd, signal_line, strict=True)
    ]
    return macd, signal_line, hist


def compute_stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 14,
    smooth: int = 3,
) -> list[float]:
    """Smoothed stochastic %K (SMA(smooth) of raw %K); nan warmup."""
    n = len(closes)
    raw = [math.nan] * n
    for i in range(k_period - 1, n):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        raw[i] = 50.0 if hh == ll else 100.0 * (closes[i] - ll) / (hh - ll)
    return compute_sma(raw, smooth)


def compute_donchian(
    highs: list[float], lows: list[float], period: int = 20
) -> tuple[list[float], list[float], list[float]]:
    """Donchian channel of the PRIOR `period` bars (current bar excluded).

    Excluding the current bar keeps breakout semantics meaningful: a close
    can then genuinely exceed the channel (a bar's own high always >= close).
    Returns (upper, lower, mid); first `period` entries are nan.
    """
    n = len(highs)
    upper = [math.nan] * n
    lower = [math.nan] * n
    mid = [math.nan] * n
    for i in range(period, n):
        u = max(highs[i - period : i])
        lo = min(lows[i - period : i])
        upper[i] = u
        lower[i] = lo
        mid[i] = (u + lo) / 2.0
    return upper, lower, mid


def compute_roc(closes: list[float], period: int = 12) -> list[float]:
    """Percent rate of change over `period` bars; nan warmup."""
    n = len(closes)
    out = [math.nan] * n
    for i in range(period, n):
        base = closes[i - period]
        out[i] = 100.0 * (closes[i] / base - 1.0) if base else math.nan
    return out


def heikin_ashi(candles: list[dict]) -> list[dict]:
    """Heikin-Ashi transform. Returns dicts with open/high/low/close."""
    out = []
    ha_open = math.nan
    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4.0
        if i == 0:
            ha_open = (c["open"] + c["close"]) / 2.0
        else:
            prev = out[-1]
            ha_open = (prev["open"] + prev["close"]) / 2.0
        out.append(
            {
                "open": ha_open,
                "high": max(c["high"], ha_open, ha_close),
                "low": min(c["low"], ha_open, ha_close),
                "close": ha_close,
            }
        )
    return out


def classic_pivots(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Classic floor-trader pivots from the previous day's H/L/C."""
    pp = (prev_high + prev_low + prev_close) / 3.0
    return {
        "pp": pp,
        "r1": 2.0 * pp - prev_low,
        "s1": 2.0 * pp - prev_high,
        "r2": pp + (prev_high - prev_low),
        "s2": pp - (prev_high - prev_low),
    }


# ------------------------- shared candle helpers -------------------------


def is_red(candle: dict) -> bool:
    return candle["close"] < candle["open"]


def is_green(candle: dict) -> bool:
    return candle["close"] > candle["open"]


def emas_stacked(ema_values: list[float], side: str) -> bool:
    """EMAs 'in sequence': fastest above slowest for LONG, inverted for SHORT.

    ema_values is ordered fastest-to-slowest, e.g. [ema8, ema21, ema55].
    """
    if any(math.isnan(v) for v in ema_values):
        return False
    if side == LONG:
        return all(ema_values[i] > ema_values[i + 1] for i in range(len(ema_values) - 1))
    return all(ema_values[i] < ema_values[i + 1] for i in range(len(ema_values) - 1))


def _closes(candles: list[dict]) -> list[float]:
    return [c["close"] for c in candles]


def _highs(candles: list[dict]) -> list[float]:
    return [c["high"] for c in candles]


def _lows(candles: list[dict]) -> list[float]:
    return [c["low"] for c in candles]


def _volumes(candles: list[dict]) -> list[float]:
    return [float(c.get("volume", 0) or 0) for c in candles]


def _today_candles(candles: list[dict]) -> list[dict]:
    """Candles sharing the signal (last) candle's calendar date, in order."""
    sig_date = _ts_date(candles[-1]["ts"])
    return [c for c in candles if _ts_date(c["ts"]) == sig_date]


def _prev_session_close(candles: list[dict]) -> float | None:
    """Close of the last candle before the signal candle's session."""
    sig_date = _ts_date(candles[-1]["ts"])
    for c in reversed(candles):
        if _ts_date(c["ts"]) != sig_date:
            return c["close"]
    return None


def _opening_range(
    candles: list[dict], minutes: int = ORB_WINDOW_MINUTES
) -> tuple[float, float] | None:
    """(high, low) of today's first `minutes` minutes.

    None when the range cannot be established or the signal candle still
    falls INSIDE the window (a breakout of a still-forming range is not one).
    """
    today = _today_candles(candles)
    if len(today) < 2:
        return None
    start = today[0]["ts"]
    sig_offset = _minutes_between(start, candles[-1]["ts"])
    if sig_offset is None or sig_offset < minutes:
        return None
    window = []
    for c in today:
        offset = _minutes_between(start, c["ts"])
        if offset is None:
            return None
        if offset < minutes:
            window.append(c)
    if not window:
        return None
    return max(c["high"] for c in window), min(c["low"] for c in window)


# ===============================================================================
# (b) VARIANT ENTRY LOGIC
#
# Every _v_* function has the uniform raw signature
#     (candles, prev_day_high, prev_day_low, side) -> dict | None
# returning {"ref": .., "stop": .., "buffer": bool} on a setup. "buffer" True
# marks a STRUCTURE stop (candle extreme / mother bar / pierce extreme): the
# shared wrapper widens it by SL_BUFFER_PCT of ref. Indicator-line stops
# (VWAP, supertrend, band mid, EMA, channel mid) use buffer False.
# The wrapper then enforces positive risk and the MAX_RISK_PCT cap.
# Insufficient bars => None, never an exception.
# ===============================================================================


def _v_ema_ribbon_trend(candles, prev_day_high, prev_day_low, side):
    """EMA 8/21/55 stacked; pullback touches EMA21; next close beyond it."""
    if len(candles) < 57:
        return None
    closes = _closes(candles)
    e8 = compute_ema(closes, 8)
    e21 = compute_ema(closes, 21)
    e55 = compute_ema(closes, 55)
    if not emas_stacked([e8[-1], e21[-1], e55[-1]], side):
        return None
    pullback, sig = candles[-2], candles[-1]
    if math.isnan(e21[-2]) or not (pullback["low"] <= e21[-2] <= pullback["high"]):
        return None
    if side == LONG:
        if sig["close"] <= pullback["high"]:
            return None
        return {"ref": sig["close"], "stop": pullback["low"], "buffer": True}
    if sig["close"] >= pullback["low"]:
        return None
    return {"ref": sig["close"], "stop": pullback["high"], "buffer": True}


def _v_vwap_breakout(candles, prev_day_high, prev_day_low, side):
    """Close crosses session VWAP with volume expansion. Stop: VWAP."""
    if len(candles) < 21:
        return None
    vwap = compute_vwap(candles)
    vol_sma = compute_sma(_volumes(candles), 20)
    prev, sig = candles[-2], candles[-1]
    if math.isnan(vwap[-1]) or math.isnan(vwap[-2]) or math.isnan(vol_sma[-1]):
        return None
    if not sig.get("volume", 0) > 1.5 * vol_sma[-1]:
        return None
    if side == LONG:
        if not (prev["close"] <= vwap[-2] and sig["close"] > vwap[-1]):
            return None
    elif not (prev["close"] >= vwap[-2] and sig["close"] < vwap[-1]):
        return None
    return {"ref": sig["close"], "stop": vwap[-1], "buffer": False}


def _v_vwap_reversion(candles, prev_day_high, prev_day_low, side):
    """Stretch > 2 stdev from VWAP, then a reversal candle back toward it."""
    if len(candles) < 22:
        return None
    vwap = compute_vwap(candles)
    dev = [
        c["close"] - v if not math.isnan(v) else math.nan
        for c, v in zip(candles, vwap, strict=True)
    ]
    std = compute_rolling_std(dev, 20)
    sig = candles[-1]
    if math.isnan(std[-2]) or std[-2] <= 0 or math.isnan(dev[-1]) or math.isnan(dev[-2]):
        return None
    if side == LONG:
        if not (dev[-2] < -2.0 * std[-2] and is_green(sig) and dev[-1] > dev[-2]):
            return None
        return {"ref": sig["close"], "stop": sig["low"], "buffer": True}
    if not (dev[-2] > 2.0 * std[-2] and is_red(sig) and dev[-1] < dev[-2]):
        return None
    return {"ref": sig["close"], "stop": sig["high"], "buffer": True}


def _v_orb_breakout(candles, prev_day_high, prev_day_low, side):
    """Close breaks the 15-minute opening range with volume expansion."""
    if len(candles) < 21:
        return None
    orb = _opening_range(candles)
    if orb is None:
        return None
    or_high, or_low = orb
    vol_sma = compute_sma(_volumes(candles), 20)
    sig = candles[-1]
    if math.isnan(vol_sma[-1]) or not sig.get("volume", 0) > 1.5 * vol_sma[-1]:
        return None
    mid = (or_high + or_low) / 2.0
    if side == LONG:
        if sig["close"] <= or_high:
            return None
    elif sig["close"] >= or_low:
        return None
    return {"ref": sig["close"], "stop": mid, "buffer": False}


def _v_supertrend_follow(candles, prev_day_high, prev_day_low, side):
    """Supertrend(10, 3) flips to the trade side on the signal candle."""
    if len(candles) < 15:
        return None
    line, direction = compute_supertrend(_highs(candles), _lows(candles), _closes(candles), 10, 3.0)
    if math.isnan(line[-1]) or direction[-2] == 0:
        return None
    sig = candles[-1]
    if side == LONG:
        if not (direction[-1] == 1 and direction[-2] == -1):
            return None
    elif not (direction[-1] == -1 and direction[-2] == 1):
        return None
    return {"ref": sig["close"], "stop": line[-1], "buffer": False}


def _v_bollinger_squeeze(candles, prev_day_high, prev_day_low, side):
    """BB(20,2) bandwidth 50-bar low on the prior candle; band breakout."""
    if len(candles) < 71:
        return None
    closes = _closes(candles)
    mid, upper, lower = compute_bollinger(closes, 20, 2.0)
    bw = [
        (u - lo) / m if not (math.isnan(m) or math.isnan(u)) and m > 0 else math.nan
        for m, u, lo in zip(mid, upper, lower, strict=True)
    ]
    window = bw[-51:-1]  # the last 50 bandwidth values ending at the prior candle
    if any(math.isnan(v) for v in window) or bw[-2] > min(window):
        return None
    sig = candles[-1]
    if side == LONG:
        if sig["close"] <= upper[-1]:
            return None
    elif sig["close"] >= lower[-1]:
        return None
    return {"ref": sig["close"], "stop": mid[-1], "buffer": False}


def _v_bollinger_reversion(candles, prev_day_high, prev_day_low, side):
    """Prior close outside BB(20,2); signal closes back inside the band."""
    if len(candles) < 22:
        return None
    closes = _closes(candles)
    mid, upper, lower = compute_bollinger(closes, 20, 2.0)
    prev, sig = candles[-2], candles[-1]
    if math.isnan(lower[-2]) or math.isnan(lower[-1]):
        return None
    if side == LONG:
        if not (prev["close"] < lower[-2] and lower[-1] <= sig["close"] <= upper[-1]):
            return None
        return {"ref": sig["close"], "stop": min(prev["low"], sig["low"]), "buffer": True}
    if not (prev["close"] > upper[-2] and lower[-1] <= sig["close"] <= upper[-1]):
        return None
    return {"ref": sig["close"], "stop": max(prev["high"], sig["high"]), "buffer": True}


def _v_macd_momentum(candles, prev_day_high, prev_day_low, side):
    """MACD cross within 2 bars, histogram building, close beyond trend EMA."""
    if len(candles) < 60:
        return None
    closes = _closes(candles)
    macd, signal_line, hist = compute_macd(closes, 12, 26, 9)
    trend = compute_ema(closes, 200) if len(closes) >= 200 else compute_ema(closes, 55)
    checks = (macd[-1], signal_line[-1], macd[-3], signal_line[-3], hist[-1], hist[-2], trend[-1])
    if any(math.isnan(v) for v in checks):
        return None
    d = 1.0 if side == LONG else -1.0

    def crossed(j: int) -> bool:
        return (macd[j] - signal_line[j]) * d > 0 and (macd[j - 1] - signal_line[j - 1]) * d <= 0

    sig = candles[-1]
    if not ((macd[-1] - signal_line[-1]) * d > 0 and (crossed(-1) or crossed(-2))):
        return None
    if not (hist[-1] * d > hist[-2] * d):  # histogram increasing toward the side
        return None
    if not ((sig["close"] - trend[-1]) * d > 0):
        return None
    stop = sig["low"] if side == LONG else sig["high"]
    return {"ref": sig["close"], "stop": stop, "buffer": True}


def _v_donchian_breakout(candles, prev_day_high, prev_day_low, side):
    """Close breaks the 20-bar Donchian channel. Stop: channel midline."""
    if len(candles) < 22:
        return None
    upper, lower, mid = compute_donchian(_highs(candles), _lows(candles), 20)
    if math.isnan(mid[-1]):
        return None
    sig = candles[-1]
    if side == LONG:
        if sig["close"] <= upper[-1]:
            return None
    elif sig["close"] >= lower[-1]:
        return None
    return {"ref": sig["close"], "stop": mid[-1], "buffer": False}


def _v_stochastic_reversal(candles, prev_day_high, prev_day_low, side):
    """Stoch(14,3) crosses out of the extreme zone with EMA(50) slope agreeing."""
    if len(candles) < 52:
        return None
    closes = _closes(candles)
    k = compute_stochastic(_highs(candles), _lows(candles), closes, 14, 3)
    e50 = compute_ema(closes, 50)
    if any(math.isnan(v) for v in (k[-1], k[-2], e50[-1], e50[-2])):
        return None
    sig = candles[-1]
    if side == LONG:
        if not (k[-2] < 20.0 <= k[-1] and e50[-1] > e50[-2]):
            return None
        return {"ref": sig["close"], "stop": sig["low"], "buffer": True}
    if not (k[-2] > 80.0 >= k[-1] and e50[-1] < e50[-2]):
        return None
    return {"ref": sig["close"], "stop": sig["high"], "buffer": True}


def _v_atr_channel_ride(candles, prev_day_high, prev_day_low, side):
    """Close beyond EMA(20) +/- 2*ATR(14) with EMA(20) sloping with the trade."""
    if len(candles) < 22:
        return None
    closes = _closes(candles)
    e20 = compute_ema(closes, 20)
    atr = compute_atr(_highs(candles), _lows(candles), closes, 14)
    if any(math.isnan(v) for v in (e20[-1], e20[-2], atr[-1])):
        return None
    sig = candles[-1]
    if side == LONG:
        if not (sig["close"] > e20[-1] + 2.0 * atr[-1] and e20[-1] > e20[-2]):
            return None
    elif not (sig["close"] < e20[-1] - 2.0 * atr[-1] and e20[-1] < e20[-2]):
        return None
    return {"ref": sig["close"], "stop": e20[-1], "buffer": False}


def _v_prev_day_level_fade(candles, prev_day_high, prev_day_low, side):
    """Intrabar pierce of the prev-day level that CLOSES back inside; fade it."""
    if len(candles) < 2 or prev_day_high <= 0 or prev_day_low <= 0:
        return None
    sig = candles[-1]
    if side == LONG:  # pierce below prev-day low, close back above it
        if not (sig["low"] < prev_day_low and sig["close"] > prev_day_low):
            return None
        return {"ref": sig["close"], "stop": sig["low"], "buffer": True}
    if not (sig["high"] > prev_day_high and sig["close"] < prev_day_high):
        return None
    return {"ref": sig["close"], "stop": sig["high"], "buffer": True}


def _v_momentum_roc(candles, prev_day_high, prev_day_low, side):
    """ROC(12) crosses the +/-1.5 pct line with a 10-bar closing extreme."""
    if len(candles) < 15:
        return None
    closes = _closes(candles)
    roc = compute_roc(closes, 12)
    if math.isnan(roc[-1]) or math.isnan(roc[-2]):
        return None
    sig = candles[-1]
    if side == LONG:
        if not (roc[-2] <= 1.5 < roc[-1] and sig["close"] >= max(closes[-10:])):
            return None
        return {"ref": sig["close"], "stop": min(_lows(candles)[-10:]), "buffer": True}
    if not (roc[-2] >= -1.5 > roc[-1] and sig["close"] <= min(closes[-10:])):
        return None
    return {"ref": sig["close"], "stop": max(_highs(candles)[-10:]), "buffer": True}


def _v_heikin_ashi_trend(candles, prev_day_high, prev_day_low, side):
    """HA color flip held 3 bars, opposite wicks shrinking bar over bar."""
    if len(candles) < 5:
        return None
    ha = heikin_ashi(candles)
    flip_bar, held = ha[-4], ha[-3:]
    if side == LONG:
        if not (flip_bar["close"] < flip_bar["open"] and all(h["close"] > h["open"] for h in held)):
            return None
        wicks = [min(h["open"], h["close"]) - h["low"] for h in held]  # lower wicks
        if not (wicks[0] >= wicks[1] >= wicks[2]):
            return None
        return {"ref": candles[-1]["close"], "stop": min(_lows(candles)[-3:]), "buffer": True}
    if not (flip_bar["close"] > flip_bar["open"] and all(h["close"] < h["open"] for h in held)):
        return None
    wicks = [h["high"] - max(h["open"], h["close"]) for h in held]  # upper wicks
    if not (wicks[0] >= wicks[1] >= wicks[2]):
        return None
    return {"ref": candles[-1]["close"], "stop": max(_highs(candles)[-3:]), "buffer": True}


def _v_volume_spike_breakout(candles, prev_day_high, prev_day_low, side):
    """Close breaks the 20-bar extreme on > 2x average volume."""
    if len(candles) < 22:
        return None
    vol_sma = compute_sma(_volumes(candles), 20)
    sig = candles[-1]
    if math.isnan(vol_sma[-1]) or not sig.get("volume", 0) > 2.0 * vol_sma[-1]:
        return None
    if side == LONG:
        if sig["close"] <= max(_highs(candles)[-21:-1]):
            return None
    elif sig["close"] >= min(_lows(candles)[-21:-1]):
        return None
    return {"ref": sig["close"], "stop": (sig["high"] + sig["low"]) / 2.0, "buffer": False}


def _v_inside_bar_breakout(candles, prev_day_high, prev_day_low, side):
    """Inside bar; signal closes beyond the mother bar. Stop: mother extreme."""
    if len(candles) < 3:
        return None
    mother, inner, sig = candles[-3], candles[-2], candles[-1]
    if not (inner["high"] <= mother["high"] and inner["low"] >= mother["low"]):
        return None
    if side == LONG:
        if sig["close"] <= mother["high"]:
            return None
        return {"ref": sig["close"], "stop": mother["low"], "buffer": True}
    if sig["close"] >= mother["low"]:
        return None
    return {"ref": sig["close"], "stop": mother["high"], "buffer": True}


def _v_engulfing_at_ema(candles, prev_day_high, prev_day_low, side):
    """Engulfing candle (body engulfs prior body) whose range touches EMA(50)."""
    if len(candles) < 52:
        return None
    e50 = compute_ema(_closes(candles), 50)
    if math.isnan(e50[-1]):
        return None
    prev, sig = candles[-2], candles[-1]
    if not (sig["low"] <= e50[-1] <= sig["high"]):
        return None
    if side == LONG:
        if not (
            is_green(sig)
            and is_red(prev)
            and sig["open"] <= prev["close"]
            and sig["close"] > prev["open"]
        ):
            return None
        return {"ref": sig["close"], "stop": sig["low"], "buffer": True}
    if not (
        is_red(sig)
        and is_green(prev)
        and sig["open"] >= prev["close"]
        and sig["close"] < prev["open"]
    ):
        return None
    return {"ref": sig["close"], "stop": sig["high"], "buffer": True}


def _v_pivot_bounce(candles, prev_day_high, prev_day_low, side):
    """Rejection of classic S1 (LONG) / R1 (SHORT) with a rejection wick."""
    if len(candles) < 2 or prev_day_high <= 0 or prev_day_low <= 0:
        return None
    prev_close = _prev_session_close(candles)
    if prev_close is None:
        return None
    pivots = classic_pivots(prev_day_high, prev_day_low, prev_close)
    sig = candles[-1]
    body = abs(sig["close"] - sig["open"])
    if side == LONG:
        wick = min(sig["open"], sig["close"]) - sig["low"]
        if not (sig["low"] <= pivots["s1"] and sig["close"] > pivots["s1"] and wick > 0):
            return None
        if wick < body:  # the rejection wick must dominate the body
            return None
        return {"ref": sig["close"], "stop": sig["low"], "buffer": True}
    wick = sig["high"] - max(sig["open"], sig["close"])
    if not (sig["high"] >= pivots["r1"] and sig["close"] < pivots["r1"] and wick > 0):
        return None
    if wick < body:
        return None
    return {"ref": sig["close"], "stop": sig["high"], "buffer": True}


def _v_gap_go(candles, prev_day_high, prev_day_low, side):
    """Opening gap > 0.5 pct; first pullback candle holding beyond the level."""
    if len(candles) < 2 or prev_day_high <= 0 or prev_day_low <= 0:
        return None
    prev_close = _prev_session_close(candles)
    today = _today_candles(candles)
    if prev_close is None or prev_close <= 0 or len(today) < 2:
        return None
    first, sig = today[0], candles[-1]
    between = today[1:-1]  # candles after the open, before the signal candle
    if side == LONG:
        if first["open"] <= prev_close * (1.0 + GAP_MIN_PCT):
            return None
        if not (is_red(sig) and sig["close"] > prev_day_high):
            return None
        if any(is_red(c) for c in between):  # signal must be the FIRST pullback
            return None
        return {"ref": sig["close"], "stop": sig["low"], "buffer": True}
    if first["open"] >= prev_close * (1.0 - GAP_MIN_PCT):
        return None
    if not (is_green(sig) and sig["close"] < prev_day_low):
        return None
    if any(is_green(c) for c in between):
        return None
    return {"ref": sig["close"], "stop": sig["high"], "buffer": True}


def _v_triple_ema_cross(candles, prev_day_high, prev_day_low, side):
    """EMA 5/13/34 freshly aligned within 3 bars, close beyond EMA(5)."""
    if len(candles) < 38:
        return None
    closes = _closes(candles)
    e5 = compute_ema(closes, 5)
    e13 = compute_ema(closes, 13)
    e34 = compute_ema(closes, 34)

    def aligned(i: int) -> bool:
        return emas_stacked([e5[i], e13[i], e34[i]], side)

    if not aligned(-1):
        return None
    if aligned(-2) and aligned(-3) and aligned(-4):  # not fresh within 3 bars
        return None
    sig = candles[-1]
    if side == LONG:
        if sig["close"] <= e5[-1]:
            return None
    elif sig["close"] >= e5[-1]:
        return None
    return {"ref": sig["close"], "stop": e34[-1], "buffer": False}


# ------------------------- shared enforcement wrapper -------------------------


def _wrap_entry(fn):
    """Wrap a raw variant with the shared safety rules.

    Applies the SL buffer to structure stops, then enforces positive risk and
    the MAX_RISK_PCT cap (risk wider than 1 percent of the reference price is
    REJECTED, mirroring the validated reference strategy). The wrapped
    callable has the uniform signature
        entry(candles, prev_day_high, prev_day_low, side) -> dict | None
    and returns {"side", "ref", "stop", "risk"} on a valid setup.
    """

    def entry(candles, prev_day_high, prev_day_low, side):
        if side not in (LONG, SHORT) or not candles:
            return None
        raw = fn(candles, prev_day_high, prev_day_low, side)
        if raw is None:
            return None
        ref = float(raw["ref"])
        stop = float(raw["stop"])
        if ref <= 0 or math.isnan(ref) or math.isnan(stop):
            return None
        if raw.get("buffer"):
            stop = stop - ref * SL_BUFFER_PCT if side == LONG else stop + ref * SL_BUFFER_PCT
        stop = round(stop, 2)
        risk = ref - stop if side == LONG else stop - ref
        if risk <= 0:
            return None
        # Hard rule: risking points must not exceed MAX_RISK_PCT of the price.
        if risk > ref * MAX_RISK_PCT:
            return None
        return {"side": side, "ref": ref, "stop": stop, "risk": round(risk, 2)}

    entry.__name__ = fn.__name__
    entry.__doc__ = fn.__doc__
    return entry


def _variant(name: str, description: str, fn) -> dict:
    return {"name": name, "description": description, "entry": _wrap_entry(fn)}


VARIANTS: dict[str, dict] = {
    "ema_ribbon_trend": _variant(
        "EMA Ribbon Trend",
        "EMA 8/21/55 stacked; pullback candle touches EMA21; next candle closes "
        "beyond the pullback extreme. Stop: pullback extreme with buffer.",
        _v_ema_ribbon_trend,
    ),
    "vwap_breakout": _variant(
        "VWAP Breakout",
        "Close crosses session VWAP (prior close on the other side) with volume "
        "above 1.5x SMA20. Stop: VWAP.",
        _v_vwap_breakout,
    ),
    "vwap_reversion": _variant(
        "VWAP Reversion",
        "Close stretched beyond 2x rolling stdev(20) from VWAP, then a reversal "
        "candle back toward VWAP. Stop: signal candle extreme.",
        _v_vwap_reversion,
    ),
    "orb_breakout": _variant(
        "Opening Range Breakout",
        "First 15 minutes define the range; close breaks it with volume above "
        "1.5x SMA20. Stop: middle of the opening range.",
        _v_orb_breakout,
    ),
    "supertrend_follow": _variant(
        "Supertrend Follow",
        "Supertrend(10,3) flips to the trade side on the signal candle. "
        "Stop: the supertrend line.",
        _v_supertrend_follow,
    ),
    "bollinger_squeeze": _variant(
        "Bollinger Squeeze Breakout",
        "BB(20,2) bandwidth at a 50-bar low on the prior candle; close breaks "
        "beyond the band. Stop: middle band.",
        _v_bollinger_squeeze,
    ),
    "bollinger_reversion": _variant(
        "Bollinger Reversion",
        "Prior close outside BB(20,2); signal closes back inside; enter toward "
        "the middle band. Stop: the outside extreme.",
        _v_bollinger_reversion,
    ),
    "macd_momentum": _variant(
        "MACD Momentum",
        "MACD(12,26,9) cross on the trade side within 2 bars, histogram "
        "building, close beyond EMA200 (EMA55 when short of bars). "
        "Stop: signal candle extreme.",
        _v_macd_momentum,
    ),
    "donchian_breakout": _variant(
        "Donchian Breakout",
        "Close breaks the 20-bar Donchian channel. Stop: channel midline.",
        _v_donchian_breakout,
    ),
    "stochastic_reversal": _variant(
        "Stochastic Reversal",
        "Stoch(14,3) crosses up from below 20 (LONG) / down from above 80 "
        "(SHORT) with EMA50 slope agreeing. Stop: signal candle extreme.",
        _v_stochastic_reversal,
    ),
    "atr_channel_ride": _variant(
        "ATR Channel Ride",
        "Close beyond EMA20 +/- 2xATR14 with EMA20 sloping in the trade "
        "direction. Stop: EMA20.",
        _v_atr_channel_ride,
    ),
    "prev_day_level_fade": _variant(
        "Previous-Day Level Fade",
        "Intrabar pierce of the previous day's high/low that closes back "
        "inside; fade the pierce. Stop: the pierce extreme.",
        _v_prev_day_level_fade,
    ),
    "momentum_roc": _variant(
        "Momentum ROC",
        "ROC(12) crosses +/-1.5 percent with the close a 10-bar extreme in the "
        "trade direction. Stop: 10-bar opposite extreme (risk-capped).",
        _v_momentum_roc,
    ),
    "heikin_ashi_trend": _variant(
        "Heikin-Ashi Trend",
        "HA color flipped 3 bars ago and held 3 bars with shrinking opposite "
        "wicks. Stop: the 3-bar raw extreme.",
        _v_heikin_ashi_trend,
    ),
    "volume_spike_breakout": _variant(
        "Volume Spike Breakout",
        "Close breaks the 20-bar high/low on volume above 2x SMA20. "
        "Stop: signal candle midpoint.",
        _v_volume_spike_breakout,
    ),
    "inside_bar_breakout": _variant(
        "Inside Bar Breakout",
        "Prior candle inside its predecessor; signal closes beyond the mother "
        "bar. Stop: mother bar opposite extreme.",
        _v_inside_bar_breakout,
    ),
    "engulfing_at_ema": _variant(
        "Engulfing at EMA",
        "Bullish/bearish engulfing whose range touches EMA50. "
        "Stop: engulfing candle extreme.",
        _v_engulfing_at_ema,
    ),
    "pivot_bounce": _variant(
        "Pivot Bounce",
        "Classic-pivot S1 (LONG) / R1 (SHORT) rejection: close back beyond the "
        "level with a dominant rejection wick. Stop: the rejection extreme.",
        _v_pivot_bounce,
    ),
    "gap_go": _variant(
        "Gap and Go",
        "Open gapped over 0.5 percent in the trade direction; first pullback "
        "candle holding beyond the prev-day level. Stop: pullback extreme.",
        _v_gap_go,
    ),
    "triple_ema_cross": _variant(
        "Triple EMA Cross",
        "EMA 5/13/34 freshly aligned within 3 bars with the close beyond EMA5. "
        "Stop: EMA34.",
        _v_triple_ema_cross,
    ),
}

DEFAULT_VARIANT = "ema_ribbon_trend"


# ===============================================================================
# (d) VARIANT RESOLUTION
# ===============================================================================


def resolve_variant_key(stem: str, env_variant: str | None = None) -> tuple[str, str | None]:
    """Resolve the active variant from a deployed filename stem.

    The deployed copy is named "<variant_key>_<YYYYmmddHHMMSS>.py"; a trailing
    _<14 digits> is stripped before the registry lookup. Fallback order:
    filename stem, VARIANT env var, then DEFAULT_VARIANT with a warning.

    Returns:
        (variant_key, warning) - warning is a message to log when the default
        fallback was used, else None.
    """
    base = re.sub(r"_\d{14}$", "", stem)
    if base in VARIANTS:
        return base, None
    env = (env_variant if env_variant is not None else os.getenv("VARIANT", "")).strip().lower()
    if env in VARIANTS:
        return env, None
    warning = (
        f"WARNING: cannot resolve a variant from filename stem {stem!r} or "
        f"VARIANT env {env!r} - FALLING BACK to default {DEFAULT_VARIANT!r}. "
        "Deploy this file as <variant_key>_<YYYYmmddHHMMSS>.py or set the "
        "VARIANT env var to one of: " + ", ".join(sorted(VARIANTS))
    )
    return DEFAULT_VARIANT, warning


# ===============================================================================
# PURE POSITION-MANAGEMENT HELPERS (shared with the reference strategy)
# ===============================================================================


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
    untouched (the initial setup stop protects the trade). From there the
    stop trails the extreme by trail_r x risk, floored at cost, and only
    ever tightens -- so a winner keeps running until the trend actually gives
    back the trail distance, capturing the whole move.
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


def check_stop(side: str, ltp: float, stop: float) -> bool:
    """True when the stop is hit."""
    if side == LONG:
        return ltp <= stop
    return ltp >= stop


def df_to_candles(df) -> list[dict]:
    """Convert a candle DataFrame into the pure layer's list-of-dicts form."""
    n = len(df)
    vols = df["volume"].tolist() if "volume" in df.columns else [0.0] * n
    return [
        {
            "ts": ts,
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
            "volume": float(v or 0),
        }
        for ts, o, h, lo, c, v in zip(
            df.index,
            df["open"].tolist(),
            df["high"].tolist(),
            df["low"].tolist(),
            df["close"].tolist(),
            vols,
            strict=True,
        )
    ]


# ===============================================================================
# (c) I/O SHELL (SDK client, scanning, orders, polling loop)
#     Copied from the validated four_ema_retracement_strategy shell.
# ===============================================================================


class VariantIntradayBot:
    def __init__(self):
        from openalgo import api  # lazy import keeps signal logic import-safe offline

        self.client = api(api_key=API_KEY, host=API_HOST, ws_url=WS_URL)
        self.variant_key, self._variant_warning = resolve_variant_key(Path(__file__).stem)
        self.variant = VARIANTS[self.variant_key]
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
        self.extreme_price = 0.0  # best price since entry (trailing anchor)
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
        # Serializes the trailing-stop read-compute-write in _risk_check (the
        # WS tick thread and the poll loop both run it). Kept separate from
        # self.lock: exit_position acquires self.lock and threading.Lock is
        # not reentrant, so a shared lock would deadlock on a stop hit.
        self._trail_lock = threading.Lock()
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
        # Seed the trailing anchor BEFORE armed becomes True: a concurrent
        # _risk_check must never observe an armed position with extreme=0.
        self.extreme_price = entry_price
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
        self.extreme_price = 0.0
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
        """Stop / breakeven / trailing / target management (WS and poll loop)."""
        if self.state == FLAT or not self.armed or price <= 0:
            return
        if check_stop(self.state, price, self.stop_price):
            self.exit_position("stoploss")
            return

        if EXIT_MODE == "TARGET":
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
            return

        # TRAIL mode: track the best price since entry, ratchet the stop.
        # Serialized under _trail_lock: without it, the WS tick thread and the
        # poll loop can interleave the stop read-compute-write and a stale
        # read would overwrite the other thread's tighter ratchet. The stop
        # check and exit_position above deliberately stay OUTSIDE this lock so
        # order placement is never performed while holding it. (The extreme is
        # only consumed by the trail ratchet, so tracking it here in TRAIL
        # mode only is equivalent.)
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
                self.initial_risk,
                self.extreme_price,
                self.stop_price,
                BREAKEVEN_R,
                TRAIL_R,
            )
            if new_stop != self.stop_price:
                # Log meaningful ratchets only, so a fast tape does not spam
                # the strategy log with sub-tick stop updates.
                if abs(new_stop - self.stop_price) >= max(0.05, 0.1 * self.initial_risk):
                    log(
                        f"{self.symbol}: trailing stop {self.stop_price:.2f} -> "
                        f"{new_stop:.2f} (extreme {self.extreme_price:.2f})"
                    )
                self.stop_price = new_stop

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
        df = self.fetch_closed_candles(symbol)
        if df is None or len(df) < 2:
            return

        candle_key = df.index[-1]
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

        candles = df_to_candles(df.iloc[-SCAN_MAX_BARS:])
        entry_fn = self.variant["entry"]
        for side in (LONG, SHORT):
            if side == LONG and direction not in ("LONG", "BOTH"):
                continue
            if side == SHORT and direction not in ("SHORT", "BOTH"):
                continue
            setup = entry_fn(candles, prev_high, prev_low, side)
            if setup:
                log(
                    f"{symbol}: {side} setup [{self.variant_key}] - "
                    f"close {setup['ref']:.2f}, stop {setup['stop']:.2f}, "
                    f"risk {setup['risk']:.2f}"
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

        if self._variant_warning:
            log(self._variant_warning)
        log(
            f"Variant resolved: {self.variant_key} ({self.variant['name']}) - "
            f"{self.variant['description']}"
        )
        # Any exchange whose watchlist resolves to nothing scans NOTHING and would
        # otherwise idle silently. Covers MCX, NFO/BFO (expiry-bearing, need the
        # resolvers), an empty screened file, and an empty WATCHLIST override.
        empty_warning = empty_watchlist_warning(EXCHANGE, _WATCHLIST_ENV, WATCHLIST)
        if empty_warning:
            log(empty_warning)

        if EXIT_MODE == "TARGET":
            exit_desc = f"fixed target {TARGET_R}R (breakeven at {BREAKEVEN_R}R)"
        else:
            if EXIT_MODE != "TRAIL":
                log(f"WARNING: invalid EXIT_MODE {EXIT_MODE!r}, using TRAIL")
            exit_desc = (
                f"trailing stop {TRAIL_R}R behind the extreme from {BREAKEVEN_R}R "
                f"(no profit cap - winners run)"
            )
        log(
            f"Variant intraday engine starting: {len(self.watchlist)} symbols on "
            f"{EXCHANGE} {CANDLE_TIMEFRAME}, variant={self.variant_key}, "
            f"risk cap {MAX_RISK_PCT * 100:.1f} pct, qty={QUANTITY}, "
            f"direction={direction}, exit: {exit_desc}, "
            f"entries until {ENTRY_CUTOFF_TIME}, square-off {SQUARE_OFF_TIME} IST"
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
                        if price <= 0 and self.armed:
                            # Degraded mode (WS dead AND quotes failing,
                            # history still up): still enforce the stop
                            # against the last closed candle so the position
                            # is not left with only the cutoff exit. STOP-HIT
                            # CHECK ONLY -- the trailing ratchet stays
                            # live-price-only and must never act on a stale
                            # candle close.
                            candles = self.fetch_closed_candles(self.symbol)
                            if candles is not None and len(candles) > 0:
                                last_close = float(candles.iloc[-1]["close"])
                                if check_stop(self.state, last_close, self.stop_price):
                                    self.exit_position("stoploss")
                elif not self.past_entry_cutoff() and not self.pending_entry:
                    for symbol in self.watchlist:
                        if self.state != FLAT:
                            break
                        self.scan_symbol(symbol, direction)
            except Exception as e:
                log(f"Cycle error: {e}")
            time.sleep(SIGNAL_CHECK_INTERVAL)


if __name__ == "__main__":
    VariantIntradayBot().run()
