#!/usr/bin/env bash
# Bootstrap a dedicated Argus SaaS deploy tree (separate from homelab mickey).
set -euo pipefail

SRC="${ARGUS_SRC:-$HOME/ai-workspace/argus-claude}"
DEST="${ARGUS_SAAS_DEST:-$HOME/ai-workspace/argus-saas}"
PORT="${ARGUS_SAAS_PORT:-8020}"

if [[ ! -d "$SRC" ]]; then
  echo "Source not found: $SRC" >&2
  exit 1
fi

gen_secret() {
  openssl rand -hex 24
}

echo "==> Argus SaaS bootstrap"
echo "    Source: $SRC"
echo "    Dest:   $DEST"
echo "    Port:   $PORT"

mkdir -p "$DEST/data"

if [[ ! -f "$DEST/.env" ]]; then
  ADMIN_TOKEN="$(gen_secret)"
  PEPPER="$(gen_secret)"
  cp "$SRC/.env.saas.example" "$DEST/.env"
  sed -i "s|CHANGE_ME_ADMIN_TOKEN|${ADMIN_TOKEN}|" "$DEST/.env"
  sed -i "s|CHANGE_ME_DISTINCT_PEPPER|${PEPPER}|" "$DEST/.env"
  sed -i "s|ARGUS_PORT=8020|ARGUS_PORT=${PORT}|" "$DEST/.env"
  sed -i "s|ARGUS_SAAS_PUBLIC_URL=http://127.0.0.1:8020|ARGUS_SAAS_PUBLIC_URL=http://127.0.0.1:${PORT}|" "$DEST/.env"
  sed -i "s|/var/lib/argus-saas/data|${DEST}/data|" "$DEST/.env"
  echo "Created $DEST/.env with generated admin token and tenant pepper."
else
  echo "Keeping existing $DEST/.env"
fi

ARGUS_DEST="$DEST" bash "$SRC/scripts/sync-deploy.sh"

if [[ ! -d "$DEST/.venv" ]]; then
  echo "==> Creating venv"
  python3 -m venv "$DEST/.venv"
  "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
fi

cat <<EOF

==> SaaS deploy ready at $DEST

Start (foreground):
  bash $DEST/scripts/start-argus-saas.sh

Or:
  cd $DEST && set -a && source .env && set +a
  .venv/bin/uvicorn app.main:app --host \${ARGUS_HOST:-0.0.0.0} --port \${ARGUS_PORT:-$PORT}

Portal:  http://127.0.0.1:${PORT}/ui/saas
Status:  http://127.0.0.1:${PORT}/saas/status

Admin token (save now — also in $DEST/.env):
  grep '^ARGUS_API_TOKEN=' "$DEST/.env"

Create first tenant:
  cd $DEST && set -a && source .env && set +a
  ARGUS_SAAS_MODE=true .venv/bin/python scripts/tenant_admin.py create platekit --name Platekit --issue-key

Stripe test setup (after adding sk_test_ key to .env):
  cd $DEST && set -a && source .env && set +a
  .venv/bin/python scripts/stripe_setup.py
  stripe listen --forward-to localhost:${PORT}/webhooks/stripe

EOF