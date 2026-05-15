#!/bin/bash
# Fetches KITE_ACCESS_TOKEN from Azure Key Vault using VM Managed Identity,
# updates .env, and restarts the trading agent so it picks up the new token.
# Runs via cron every 5 min from 08:00–09:00 IST (Mon–Fri).

set -euo pipefail

VAULT_NAME="tradingkvlje77ohmfrs5c"
SECRET_NAME="KITE-ACCESS-TOKEN"
ENV_FILE="/opt/self-learning-trading-agent/.env"
SERVICE="self-learning-trading-agent"
LOG_FILE="/opt/self-learning-trading-agent/logs/kite-token.log"

mkdir -p "$(dirname "$LOG_FILE")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S IST') $*" | tee -a "$LOG_FILE"; }

# ── 1. Get Managed Identity access token for Key Vault ───────────────────────
MI_RESPONSE=$(curl -sf \
  -H "Metadata: true" \
  "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net")

MI_TOKEN=$(echo "$MI_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || true)

if [ -z "$MI_TOKEN" ]; then
  log "ERROR: Failed to get Managed Identity token"
  exit 1
fi

# ── 2. Fetch secret from Key Vault ───────────────────────────────────────────
KV_RESPONSE=$(curl -sf \
  -H "Authorization: Bearer $MI_TOKEN" \
  "https://${VAULT_NAME}.vault.azure.net/secrets/${SECRET_NAME}?api-version=7.4")

NEW_TOKEN=$(echo "$KV_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['value'])" 2>/dev/null || true)

if [ -z "$NEW_TOKEN" ]; then
  log "ERROR: Failed to fetch secret '$SECRET_NAME' from Key Vault '$VAULT_NAME'"
  exit 1
fi

# ── 3. Compare with current token — skip restart if unchanged ────────────────
CURRENT_TOKEN=$(grep "^KITE_ACCESS_TOKEN=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- || true)

if [ "$NEW_TOKEN" = "$CURRENT_TOKEN" ]; then
  log "Token unchanged — no action needed"
  exit 0
fi

# ── 4. Update .env ───────────────────────────────────────────────────────────
if grep -q "^KITE_ACCESS_TOKEN=" "$ENV_FILE"; then
  sed -i "s|^KITE_ACCESS_TOKEN=.*|KITE_ACCESS_TOKEN=${NEW_TOKEN}|" "$ENV_FILE"
else
  echo "KITE_ACCESS_TOKEN=${NEW_TOKEN}" >> "$ENV_FILE"
fi
log "KITE_ACCESS_TOKEN updated in .env"

# ── 5. Restart service so it loads the new token ─────────────────────────────
systemctl restart "$SERVICE"
log "Service '$SERVICE' restarted with new token"
