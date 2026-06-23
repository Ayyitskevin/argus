#!/usr/bin/env bash
# Post-export hook for Capture One → Argus analyze + local sidecars.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FOLDER="${1:-}"

if [[ -z "$FOLDER" || ! -d "$FOLDER" ]]; then
  echo "Usage: $0 /path/to/exported/gallery" >&2
  exit 1
fi

PYTHON="${ARGUS_PYTHON:-python3}"
SCRIPT="${ARGUS_SCRIPT:-$ROOT/docs/lightroom_export_stub.py}"
BASE_URL="${ARGUS_BASE_URL:-http://127.0.0.1:8010}"
LIMIT="${ARGUS_LIMIT:-20}"
TOKEN="${ARGUS_API_TOKEN:-}"
CLIENT_ID="${ARGUS_CLIENT_ID:-}"
RECURSIVE="${ARGUS_RECURSIVE:-false}"

ARGS=(
  "$SCRIPT"
  "$FOLDER"
  --base-url "$BASE_URL"
  --limit "$LIMIT"
  --target-dir "$FOLDER"
  --manifest-out "$FOLDER/argus-manifest.json"
)

if [[ -n "$TOKEN" ]]; then
  ARGS+=(--token "$TOKEN")
fi
if [[ -n "$CLIENT_ID" ]]; then
  ARGS+=(--client-id "$CLIENT_ID")
fi
if [[ "$RECURSIVE" == "true" || "$RECURSIVE" == "1" ]]; then
  ARGS+=(--recursive)
fi

exec "$PYTHON" "${ARGS[@]}"