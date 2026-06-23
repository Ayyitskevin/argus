#!/usr/bin/env bash
# Install or refresh argus systemd unit on mickey (or any Linux host).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
UNIT_DEST=/etc/systemd/system/argus.service
ENV_FILE="$ROOT/.env"

echo "==> Argus install from $ROOT"

if [[ ! -f "$ROOT/.venv/bin/uvicorn" ]]; then
  echo "Missing venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e ."
  exit 1
fi

if [[ -f "$ROOT/data/argus.db" ]]; then
  BACKUP="$ROOT/data/backups/argus-predeploy-$(date +%Y%m%d-%H%M%S).db"
  mkdir -p "$ROOT/data/backups"
  cp -a "$ROOT/data/argus.db" "$BACKUP"
  echo "==> DB backup: $BACKUP"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  echo "==> Created $ENV_FILE from .env.example (review before production)"
fi

sudo cp "$ROOT/argus.service" "$UNIT_DEST"
sudo systemctl daemon-reload
sudo systemctl enable argus
sudo systemctl restart argus
sleep 1
systemctl is-active argus
curl -sf "http://127.0.0.1:${ARGUS_PORT:-8010}/healthz" | head -c 200
echo
echo "==> Argus installed (check .env for ARGUS_VISION_BACKEND)"