#!/usr/bin/env bash
# Sync argus-claude (git) → argus (mickey deploy tree). Preserves .env and data.
set -euo pipefail

SRC="${ARGUS_SRC:-$HOME/ai-workspace/argus-claude}"
DEST="${ARGUS_DEST:-$HOME/ai-workspace/argus}"

if [[ ! -d "$SRC" ]]; then
  echo "Source not found: $SRC" >&2
  exit 1
fi
mkdir -p "$DEST"

echo "Syncing $SRC → $DEST"
rsync -av --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache/' \
  --exclude 'data/' \
  --exclude '.env' \
  "$SRC/" "$DEST/"

echo "Done. Restart uvicorn or argus.service to pick up changes."
echo "  curl -s http://127.0.0.1:8010/vision/status | jq ."