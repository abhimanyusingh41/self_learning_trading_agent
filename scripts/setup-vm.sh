#!/bin/bash
# One-time VM bootstrap — run this ONCE manually on the Azure VM.
# After this, all future deploys happen automatically via GitHub Actions.
#
# Usage:
#   ssh user@vm-host
#   bash setup-vm.sh <GITHUB_PAT>
#
# What this does:
#   1. Installs system dependencies
#   2. Clones repo to /opt/self-learning-trading-agent/
#   3. Migrates existing trading-agent / trading-dashboard services → new names
#   4. Installs self-learning-trading-agent.service
#      and   self-learning-trading-dashboard.service
#   5. Creates .env template if not present
#
# What it does NOT touch:
#   - Any other project or service on this VM
#   - /opt/trading-bot/ or trading-bot.service / dashboard.service

set -euo pipefail

GH_PAT="${1:-}"
if [ -z "$GH_PAT" ]; then
  echo "Usage: bash setup-vm.sh <GITHUB_PAT>"
  exit 1
fi

DEPLOY_DIR="/opt/self-learning-trading-agent"
REPO_URL="https://${GH_PAT}@github.com/abhimanyusingh41/zerodha-trading-agent.git"
SERVICE_AGENT="self-learning-trading-agent"
SERVICE_DASH="self-learning-trading-dashboard"
OLD_SERVICE_AGENT="trading-agent"
OLD_SERVICE_DASH="trading-dashboard"

echo "========================================"
echo " Self-Learning Trading Agent — VM Setup"
echo " Deploy dir : $DEPLOY_DIR"
echo " Run as     : $USER"
echo "========================================"

# ── 1. System dependencies ───────────────────────────────────────────────────
echo "[1/6] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-dev git -qq
echo "      Done."

# ── 2. Clone or update repo ──────────────────────────────────────────────────
echo "[2/6] Cloning repository to $DEPLOY_DIR..."
if [ -d "$DEPLOY_DIR/.git" ]; then
  echo "      Repo already exists — pulling latest..."
  cd "$DEPLOY_DIR"
  git remote set-url origin "$REPO_URL"
  git fetch origin main --quiet
  git reset --hard origin/main
else
  sudo git clone "$REPO_URL" "$DEPLOY_DIR"
  sudo chown -R "$USER:$USER" "$DEPLOY_DIR"
fi
echo "      Commit: $(git -C $DEPLOY_DIR log -1 --oneline)"

# ── 3. Install Python dependencies ───────────────────────────────────────────
echo "[3/6] Installing Python dependencies..."
pip3 install -r "$DEPLOY_DIR/requirements.txt" --quiet
echo "      Done."

# ── 4. Create .env if missing ────────────────────────────────────────────────
echo "[4/6] Setting up .env..."
ENV_FILE="$DEPLOY_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  echo "      .env already exists — not overwriting."
else
  sudo tee "$ENV_FILE" > /dev/null << 'EOF'
# Self-Learning Trading Agent — secrets (never committed to git)
ANTHROPIC_API_KEY=

KITE_API_KEY=
KITE_API_SECRET=
KITE_ACCESS_TOKEN=

BINANCE_API_KEY=
BINANCE_API_SECRET=
EOF
  sudo chown "$USER:$USER" "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
  echo "      Created .env template — fill in your API keys before starting."
fi

mkdir -p "$DEPLOY_DIR/logs"

# ── 5. Migrate old service names → new names ─────────────────────────────────
echo "[5/6] Migrating service names..."

for OLD_SVC in "$OLD_SERVICE_AGENT" "$OLD_SERVICE_DASH"; do
  if systemctl list-units --full --all | grep -q "${OLD_SVC}.service"; then
    echo "      Found old service: ${OLD_SVC} — stopping and disabling..."
    sudo systemctl stop  "${OLD_SVC}.service"  2>/dev/null || true
    sudo systemctl disable "${OLD_SVC}.service" 2>/dev/null || true
    echo "      Stopped and disabled ${OLD_SVC}.service"
  else
    echo "      ${OLD_SVC}.service not found — nothing to migrate."
  fi
done

# ── 6. Install new service files ─────────────────────────────────────────────
echo "[6/6] Installing systemd services..."

install_service() {
  local SRC="$DEPLOY_DIR/scripts/${1}.service"
  local DEST="/etc/systemd/system/${1}.service"
  sed "s/__VM_USER__/$USER/g" "$SRC" | sudo tee "$DEST" > /dev/null
  echo "      Installed $DEST"
}

install_service "$SERVICE_AGENT"
install_service "$SERVICE_DASH"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_AGENT}.service"
sudo systemctl enable "${SERVICE_DASH}.service"
echo "      Both services enabled (start on reboot)."

echo ""
echo "========================================"
echo " Setup complete!"
echo ""
if ! grep -q "ANTHROPIC_API_KEY=." "$ENV_FILE" 2>/dev/null; then
  echo " *** ACTION REQUIRED: fill in API keys ***"
  echo "   nano $ENV_FILE"
  echo ""
fi
echo " Start services:"
echo "   sudo systemctl start $SERVICE_AGENT"
echo "   sudo systemctl start $SERVICE_DASH"
echo ""
echo " Check logs:"
echo "   sudo journalctl -u $SERVICE_AGENT -f"
echo "   tail -f $DEPLOY_DIR/logs/agent.log"
echo ""
echo " Future deploys: push to main — GitHub Actions handles everything."
echo "========================================"
