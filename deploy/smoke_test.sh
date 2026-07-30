#!/usr/bin/env bash
#
# Post-deploy sandbox order-flow smoke test.
#
# Proves the deployed instance can actually take an order end to end - not just
# that the homepage returns 200. It walks: ping -> analyzer mode -> funds ->
# quote -> place order -> orderbook -> positionbook -> close out.
#
# Usage:
#   export OPENALGO_URL=https://your.domain
#   export OPENALGO_APIKEY=...        # from /apikey on the deployed instance
#   ./deploy/smoke_test.sh
#
# Optional:
#   SMOKE_SYMBOL   (default SBIN)     SMOKE_EXCHANGE (default NSE)
#   SMOKE_QTY      (default 1)        SMOKE_PRODUCT  (default MIS)
#   SMOKE_STRATEGY (default SMOKETEST)
#
# ---------------------------------------------------------------------------
# SAFETY: this script REFUSES TO PLACE ANY ORDER unless the instance reports
# analyzer (sandbox) mode is ON. That check is not a formality - with analyzer
# off, /api/v1/placeorder sends a REAL order to your broker with real money.
# The check is fail-closed: an unreachable or unparseable analyzer response
# aborts before the order step.
#
# The API key is read from the environment and never echoed. Rotate it at
# /apikey afterwards if this ran anywhere you do not fully control.
# ---------------------------------------------------------------------------

set -uo pipefail

URL="${OPENALGO_URL:-}"
KEY="${OPENALGO_APIKEY:-}"
SYMBOL="${SMOKE_SYMBOL:-SBIN}"
EXCHANGE="${SMOKE_EXCHANGE:-NSE}"
QTY="${SMOKE_QTY:-1}"
PRODUCT="${SMOKE_PRODUCT:-MIS}"
STRATEGY="${SMOKE_STRATEGY:-SMOKETEST}"

pass=0
fail=0
placed_orderid=""

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
ok()   { pass=$((pass + 1)); printf '  [PASS] %s\n' "$*"; }
bad()  { fail=$((fail + 1)); printf '  [FAIL] %s\n' "$*"; }
die()  { printf '\nABORT: %s\n' "$*" >&2; exit 1; }

[ -n "$URL" ] || die "OPENALGO_URL is not set."
[ -n "$KEY" ] || die "OPENALGO_APIKEY is not set."
command -v curl >/dev/null || die "curl not found."
command -v python3 >/dev/null || die "python3 not found (used to parse JSON)."

URL="${URL%/}"

# POST a JSON body to an /api/v1 path. Body is passed on stdin so the API key
# never appears in the process list.
api() {
    local path="$1" body="$2"
    printf '%s' "$body" | curl -sS --max-time 30 \
        -H 'Content-Type: application/json' \
        --data-binary @- \
        "$URL/api/v1/$path" 2>/dev/null
}

# Extract a dotted path from JSON on stdin. Prints nothing if absent.
jget() {
    python3 -c '
import json, sys
try:
    cur = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for part in sys.argv[1].split("."):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        sys.exit(0)
print("" if cur is None else cur)
' "$1" 2>/dev/null
}

say "OpenAlgo smoke test"
say "  target   : $URL"
say "  symbol   : $SYMBOL ($EXCHANGE)  qty $QTY  product $PRODUCT"
say "  strategy : $STRATEGY"

# --------------------------------------------------------------------------
step "1. Reachability"
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$URL/" || echo 000)
if [ "$code" != "000" ] && [ "$code" -lt 500 ]; then
    ok "app responds (HTTP $code)"
else
    die "app is not responding at $URL (HTTP $code). Check: systemctl status openalgo"
fi

resp=$(api "ping" "{\"apikey\":\"$KEY\"}")
if [ "$(printf '%s' "$resp" | jget status)" = "success" ]; then
    ok "API key accepted (/api/v1/ping)"
else
    die "API key rejected or API unreachable. Response: ${resp:0:200}"
fi

# --------------------------------------------------------------------------
step "2. Analyzer (sandbox) mode - MUST be ON before any order"
resp=$(api "analyzer" "{\"apikey\":\"$KEY\"}")
analyze=$(printf '%s' "$resp" | jget data.analyze_mode)
mode=$(printf '%s' "$resp" | jget data.mode)

case "$analyze" in
    True|true)
        ok "analyzer mode is ON (mode=$mode) - orders will be simulated"
        ;;
    False|false)
        die "analyzer mode is OFF (mode=$mode).
       Refusing to continue: an order placed now would be a REAL order with
       real money. Turn sandbox on at $URL/sandbox, then re-run."
        ;;
    *)
        die "could not read analyzer mode from the response - failing closed
       rather than risking a live order. Response: ${resp:0:200}"
        ;;
esac

# --------------------------------------------------------------------------
step "3. Broker session and market data"
resp=$(api "funds" "{\"apikey\":\"$KEY\"}")
if [ "$(printf '%s' "$resp" | jget status)" = "success" ]; then
    cash=$(printf '%s' "$resp" | jget data.availablecash)
    ok "funds readable (available cash: ${cash:-unknown})"
else
    bad "funds unavailable - broker session may not be logged in. ${resp:0:160}"
fi

resp=$(api "quotes" "{\"apikey\":\"$KEY\",\"symbol\":\"$SYMBOL\",\"exchange\":\"$EXCHANGE\"}")
ltp=$(printf '%s' "$resp" | jget data.ltp)
if [ -n "$ltp" ]; then
    ok "live quote for $SYMBOL: $ltp"
else
    bad "no quote for $SYMBOL - master contract may not be loaded. ${resp:0:160}"
fi

# --------------------------------------------------------------------------
step "4. Place a simulated BUY"
resp=$(api "placeorder" "{\"apikey\":\"$KEY\",\"strategy\":\"$STRATEGY\",\"symbol\":\"$SYMBOL\",\"exchange\":\"$EXCHANGE\",\"action\":\"BUY\",\"quantity\":$QTY,\"pricetype\":\"MARKET\",\"product\":\"$PRODUCT\"}")
status=$(printf '%s' "$resp" | jget status)
placed_orderid=$(printf '%s' "$resp" | jget orderid)

if [ "$status" = "success" ] && [ -n "$placed_orderid" ]; then
    ok "BUY accepted, orderid $placed_orderid"
else
    msg=$(printf '%s' "$resp" | jget message)
    # A capital-guard rejection is a correct, informative outcome - report it
    # as such rather than as a broken deploy.
    case "$msg" in
        *[Cc]apital*|*max_positions*|*position\ limit*)
            bad "order rejected by the capital guard: $msg
         (this means the guard is ON and working - raise the allocation or
          disable the guard at /settings/broker to complete the smoke test)"
            ;;
        *) bad "order rejected: ${msg:-${resp:0:200}}" ;;
    esac
fi

# --------------------------------------------------------------------------
step "5. Order and position books reflect it"
sleep 2

resp=$(api "orderbook" "{\"apikey\":\"$KEY\"}")
if printf '%s' "$resp" | grep -q "$STRATEGY"; then
    ok "orderbook contains the $STRATEGY order"
else
    bad "orderbook does not show the $STRATEGY order"
fi

resp=$(api "positionbook" "{\"apikey\":\"$KEY\"}")
if printf '%s' "$resp" | grep -q "$SYMBOL"; then
    ok "positionbook shows $SYMBOL"
else
    bad "positionbook does not show $SYMBOL (may be fine if the fill is pending)"
fi

# --------------------------------------------------------------------------
step "6. Close the position out"
# Always attempt the exit, even if earlier steps failed - a smoke test must not
# leave a position behind.
if [ -n "$placed_orderid" ]; then
    resp=$(api "placeorder" "{\"apikey\":\"$KEY\",\"strategy\":\"$STRATEGY\",\"symbol\":\"$SYMBOL\",\"exchange\":\"$EXCHANGE\",\"action\":\"SELL\",\"quantity\":$QTY,\"pricetype\":\"MARKET\",\"product\":\"$PRODUCT\"}")
    if [ "$(printf '%s' "$resp" | jget status)" = "success" ]; then
        ok "position squared off"
    else
        bad "SQUARE-OFF FAILED - check $URL/positions and close manually. ${resp:0:200}"
    fi
else
    say "  (nothing to close - no order was placed)"
fi

# --------------------------------------------------------------------------
step "Result"
say "  passed: $pass    failed: $fail"
if [ "$fail" -gt 0 ]; then
    say "
Smoke test FAILED. This was sandbox mode, so nothing real was traded, but do
not enable live trading until these pass. Check log/errors.jsonl on the server
and 'journalctl -u openalgo -n 100'."
    exit 1
fi
say "
Smoke test PASSED - the deployed instance takes orders end to end in sandbox.
Still to do before live trading:
  1. Whitelist the server's STATIC IP with your broker (SEBI mandate).
  2. Run a full sandbox session during market hours and read /performance.
  3. Only then consider turning analyzer mode off."
exit 0
