#!/usr/bin/env bash
#
# One-shot installer for the 5G network quality monitor on a Raspberry Pi.
#
# Usage (run as your normal user, e.g. pi — NOT as root):
#   ./scripts/install.sh               # core monitor (recommended)
#   ./scripts/install.sh --charts      # + matplotlib charts in reports
#   ./scripts/install.sh --tailscale   # + install Tailscale (auth is interactive)
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="5g-network-test"
INSTALL_TAILSCALE=0
INSTALL_CHARTS=0
for arg in "$@"; do
  case "$arg" in
    --tailscale) INSTALL_TAILSCALE=1 ;;
    --charts)    INSTALL_CHARTS=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  echo "ERROR: run this script as your normal user (e.g. pi), not as root." >&2
  exit 1
fi

RUN_USER="$(id -un)"
PY=".venv/bin/python"

echo "==> [1/6] Installing system packages (sudo needed)..."
sudo apt-get update
sudo apt-get install -y iputils-ping curl python3 python3-venv python3-pip git

echo "==> [2/6] Installing Ookla speedtest CLI..."
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-aarch64.tgz" ;;
  armv7l|armhf)  URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-armhf.tgz" ;;
  x86_64|amd64)  URL="https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz" ;;
  *) echo "  No Ookla build for arch '$ARCH'; using Python speedtest-cli fallback."; URL="" ;;
esac
if [[ -n "$URL" ]]; then
  TMP="$(mktemp -d)"
  if curl -fsSL "$URL" -o "$TMP/ookla.tgz" && tar -xzf "$TMP/ookla.tgz" -C "$TMP" && [[ -x "$TMP/speedtest" ]]; then
    sudo install -m 755 "$TMP/speedtest" /usr/local/bin/speedtest
    echo "  Ookla speedtest installed at /usr/local/bin/speedtest"
  else
    echo "  WARNING: Ookla download failed; will use Python speedtest-cli fallback."
  fi
  rm -rf "$TMP"
fi

echo "==> [3/6] Creating Python virtualenv..."
cd "$REPO_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install speedtest-cli
if [[ "$INSTALL_CHARTS" -eq 1 ]]; then
  ./.venv/bin/pip install matplotlib
fi

echo "==> [4/6] Quick self-test (one ping round + one speedtest)..."
if "$PY" -m nettest.main --once --config "$REPO_DIR/config.json"; then
  echo "  Self-test OK."
else
  echo "  WARNING: self-test reported problems (see output above)."
fi

echo "==> [5/6] Installing systemd service..."
sed -e "s|__USER__|$RUN_USER|g" -e "s|__REPO_DIR__|$REPO_DIR|g" \
  "$REPO_DIR/systemd/$SERVICE.service" \
  | sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
echo "  Service enabled and started."

echo "==> [6/6] Tailscale"
if [[ "$INSTALL_TAILSCALE" -eq 1 ]]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  echo "  Tailscale installed. Authenticate (one-time, interactive):"
  echo "    sudo tailscale up --ssh"
  echo "  then open the printed URL in a browser and log in."
else
  echo "  Skipped. Install later with:"
  echo "    curl -fsSL https://tailscale.com/install.sh | sh"
  echo "    sudo tailscale up --ssh"
fi

echo
echo "Done. Useful commands:"
echo "  systemctl status $SERVICE"
echo "  journalctl -u $SERVICE -f"
echo "  tail -f $REPO_DIR/logs/ping_$(date +%Y%m%d).csv"
echo "  $PY -m nettest.main --report --config $REPO_DIR/config.json"
echo "  cat $REPO_DIR/status.json"
