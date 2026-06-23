#!/usr/bin/env bash
# Start Argus SaaS instance (expects .env in deploy tree).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env — run: bash scripts/saas-bootstrap.sh" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ "${ARGUS_SAAS_MODE:-false}" != "true" ]]; then
  echo "ARGUS_SAAS_MODE must be true for this script" >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

HOST="${ARGUS_HOST:-0.0.0.0}"
PORT="${ARGUS_PORT:-8020}"
echo "Starting Argus SaaS on ${HOST}:${PORT}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"