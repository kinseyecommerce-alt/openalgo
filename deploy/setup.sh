#!/usr/bin/env bash
#
# One-shot first-boot setup for OpenAlgo on a fresh Ubuntu VM
# (AWS Lightsail / EC2 / any Ubuntu 24.04 host).
#
# Run ON THE SERVER, once:
#   curl -fsSL https://raw.githubusercontent.com/<owner>/openalgo/<branch>/deploy/setup.sh -o setup.sh
#   less setup.sh          # read it before running anything as root
#   sudo bash setup.sh --repo https://github.com/<owner>/openalgo --branch <branch>
#
# It installs system packages, uv, nginx, clones the repo to /opt/openalgo,
# and installs the systemd unit. It deliberately does NOT write your .env or
# start trading - you do that by hand afterwards, so credentials never pass
# through a script or CI.
#
# Idempotent: safe to re-run.

set -euo pipefail

REPO="https://github.com/marketcalls/openalgo"
BRANCH="main"
APP_DIR="/opt/openalgo"
APP_USER="ubuntu"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)   REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --dir)    APP_DIR="$2"; shift 2 ;;
        --user)   APP_USER="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

log() { echo "[setup] $*"; }

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi
if ! id "$APP_USER" &>/dev/null; then
    echo "User '$APP_USER' does not exist. Pass --user <name>." >&2
    exit 1
fi

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git curl ca-certificates build-essential pkg-config \
    python3 python3-venv python3-dev \
    nginx ufw

log "Installing uv for $APP_USER"
# uv manages Python 3.12 and the venv; never use the system Python directly.
sudo -u "$APP_USER" bash -lc '
    if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
'

log "Fetching $REPO ($BRANCH) into $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
else
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

log "Installing Python dependencies"
sudo -u "$APP_USER" bash -lc "cd '$APP_DIR' && \$HOME/.local/bin/uv sync"

log "Installing systemd unit"
sed -e "s#/opt/openalgo#$APP_DIR#g" \
    -e "s#User=ubuntu#User=$APP_USER#" \
    -e "s#Group=ubuntu#Group=$APP_USER#" \
    -e "s#/home/ubuntu#/home/$APP_USER#g" \
    "$APP_DIR/deploy/openalgo.service" > /etc/systemd/system/openalgo.service
systemctl daemon-reload
systemctl enable openalgo

log "Installing nginx site"
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/openalgo
ln -sf /etc/nginx/sites-available/openalgo /etc/nginx/sites-enabled/openalgo
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

log "Configuring firewall"
# The app (5000) and the WebSocket proxy (8765) are NOT exposed directly -
# nginx fronts both. Only SSH and HTTP(S) are open.
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null
log "Firewall: SSH + HTTP/HTTPS open; 5000 and 8765 stay loopback-only behind nginx"

cat <<EOF

[setup] Base install complete. Remaining steps are MANUAL and deliberately so:

  1. Create the .env (never commit it, never put it in CI):
       cd $APP_DIR
       cp .sample.env .env
       # Generate fresh secrets:
       \$HOME/.local/bin/uv run python -c "import secrets; print(secrets.token_hex(32))"
       # Set at minimum: APP_KEY, API_KEY_PEPPER, BROKER_API_KEY,
       # BROKER_API_SECRET, and REDIRECT_URL for your broker, e.g.
       #   REDIRECT_URL = 'http://<your-domain>/zerodha/callback'

  2. Start it:
       sudo systemctl start openalgo
       sudo systemctl status openalgo
       journalctl -u openalgo -f

  3. Add TLS once DNS points here:
       sudo certbot --nginx -d your.domain

  4. BEFORE trading: whitelist this server's STATIC IP with your broker
     (SEBI mandate). Then log in, turn Analyzer/Sandbox mode ON at /sandbox,
     and CONFIRM it is on before starting any strategy.

EOF
