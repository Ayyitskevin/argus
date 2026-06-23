#!/usr/bin/env bash
# Enable Plutus upsell hand-off on flow Mise (gallery admin tiles + job worker).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLOW_HOST="${MISE_FLOW_HOST:-flow}"
MISE_ENV="${MISE_ENV_PATH:-/opt/mise/.env}"
PLUTUS_HOST="${MISE_PLUTUS_HOST:-strix-halo-a9-mega}"
PLUTUS_PORT="${MISE_PLUTUS_PORT:-8030}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

TOKEN="${MISE_PLUTUS_TOKEN:-${ARGUS_PLUTUS_TOKEN:-${ARGUS_API_TOKEN:-}}}"
if [[ -z "$TOKEN" ]]; then
  echo "Set MISE_PLUTUS_TOKEN or ARGUS_API_TOKEN in $ROOT/.env" >&2
  exit 1
fi

PLUTUS_URL="${MISE_PLUTUS_URL:-http://${PLUTUS_HOST}:${PLUTUS_PORT}}"

echo "==> Deploy Plutus config to $FLOW_HOST:$MISE_ENV"
echo "    MISE_PLUTUS_URL=$PLUTUS_URL"

ssh "$FLOW_HOST" "bash -s" <<REMOTE
set -euo pipefail
ENV="$MISE_ENV"
touch "\$ENV"
grep -q '^MISE_PLUTUS_URL=' "\$ENV" && sed -i 's|^MISE_PLUTUS_URL=.*|MISE_PLUTUS_URL=$PLUTUS_URL|' "\$ENV" || echo "MISE_PLUTUS_URL=$PLUTUS_URL" >> "\$ENV"
grep -q '^MISE_PLUTUS_TOKEN=' "\$ENV" && sed -i 's|^MISE_PLUTUS_TOKEN=.*|MISE_PLUTUS_TOKEN=$TOKEN|' "\$ENV" || echo "MISE_PLUTUS_TOKEN=$TOKEN" >> "\$ENV"
grep -q '^MISE_PLUTUS_TIMEOUT=' "\$ENV" || echo "MISE_PLUTUS_TIMEOUT=60" >> "\$ENV"
echo "==> Mise .env Plutus lines:"
grep MISE_PLUTUS "\$ENV" || true
REMOTE

echo ""
echo "==> .env updated on $FLOW_HOST"
echo "    Restart Mise on flow (needs sudo once):"
echo "      ssh $FLOW_HOST 'sudo systemctl restart mise'"
echo "    Then verify: curl -s http://flow:8400/api/galleries?published=true -H \"Authorization: Bearer ...\" | jq .galleries[0].plutus_last_status"