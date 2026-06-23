#!/usr/bin/env bash
# Sync argus-claude → deploy trees, restart services, smoke-check health + pytest.
set -euo pipefail

SRC="${ARGUS_SRC:-$HOME/ai-workspace/argus-claude}"
HOMELAB_DEST="${ARGUS_HOMELAB_DEST:-$HOME/ai-workspace/argus}"
SAAS_DEST="${ARGUS_SAAS_DEST:-$HOME/ai-workspace/argus-saas}"
HOMELAB_PORT="${ARGUS_HOMELAB_PORT:-8010}"
SAAS_PORT="${ARGUS_SAAS_PORT:-8020}"

echo "==> Sync homelab deploy"
ARGUS_DEST="$HOMELAB_DEST" bash "$SRC/scripts/sync-deploy.sh"

echo "==> Sync SaaS deploy"
ARGUS_DEST="$SAAS_DEST" bash "$SRC/scripts/sync-deploy.sh"

echo "==> Restart homelab :$HOMELAB_PORT"
if lsof -t -i:"$HOMELAB_PORT" >/dev/null 2>&1; then
  kill "$(lsof -t -i:"$HOMELAB_PORT" | head -1)" 2>/dev/null || true
  sleep 1
fi
cd "$HOMELAB_DEST"
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$HOMELAB_PORT" >/tmp/argus-homelab.log 2>&1 &
sleep 2

echo "==> Restart SaaS :$SAAS_PORT"
if lsof -t -i:"$SAAS_PORT" >/dev/null 2>&1; then
  kill "$(lsof -t -i:"$SAAS_PORT" | head -1)" 2>/dev/null || true
  sleep 1
fi
cd "$SAAS_DEST"
if [[ -f .env ]]; then
  nohup bash scripts/start-argus-saas.sh >/tmp/argus-saas.log 2>&1 &
  sleep 2
else
  echo "WARN: $SAAS_DEST/.env missing — skipping SaaS restart"
fi

echo "==> Health checks"
curl -sf "http://127.0.0.1:$HOMELAB_PORT/healthz" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('checks'), d; print('homelab', d['status'])"
if curl -sf "http://127.0.0.1:$SAAS_PORT/healthz" >/dev/null 2>&1; then
  curl -sf "http://127.0.0.1:$SAAS_PORT/healthz" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('saas_mode'); print('saas', d['status'])"
fi

echo "==> pytest (mock vision)"
cd "$SRC"
ARGUS_VISION_BACKEND=mock ARGUS_QUEUE_ENABLED=false .venv/bin/python -m pytest -q

echo "==> deploy-smoke OK"