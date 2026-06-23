#!/usr/bin/env bash
# Interim Argus start when systemd install needs sudo (Phase 5).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export ARGUS_DATA_DIR="${ARGUS_DATA_DIR:-$ROOT/data}"
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created $ROOT/.env — review before production"
fi
exec "$ROOT/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "${ARGUS_PORT:-8010}"