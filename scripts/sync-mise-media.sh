#!/usr/bin/env bash
# Rsync published Mise gallery originals from flow → local ARGUS_MISE_MEDIA_ROOT.
# Safe to cron; incremental. Does not delete remote-only files by default.
set -euo pipefail

FLOW_HOST="${MISE_FLOW_HOST:-flow}"
REMOTE_ROOT="${MISE_REMOTE_MEDIA:-/opt/mise/data/media}"
LOCAL_ROOT="${ARGUS_MISE_MEDIA_ROOT:-$HOME/ai-workspace/argus/data/mise-media}"
TOKEN="${ARGUS_MISE_API_TOKEN:-}"
MISE_URL="${ARGUS_MISE_URL:-http://flow:8400}"
SYNC_ALL="${MISE_SYNC_ALL:-false}"

mkdir -p "$LOCAL_ROOT"

sync_gallery() {
  local gid="$1"
  local dest="$LOCAL_ROOT/$gid/original"
  mkdir -p "$dest"
  echo "==> rsync gallery $gid → $dest"
  rsync -a "${FLOW_HOST}:${REMOTE_ROOT}/${gid}/original/" "$dest/"
}

if [[ "$SYNC_ALL" == "true" || "$SYNC_ALL" == "1" ]]; then
  if [[ -z "$TOKEN" ]]; then
    echo "ARGUS_MISE_API_TOKEN required for MISE_SYNC_ALL" >&2
    exit 1
  fi
  mapfile -t IDS < <(curl -sf -H "Authorization: Bearer $TOKEN" \
    "$MISE_URL/api/galleries?published=true" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('\n'.join(str(g['id']) for g in d.get('galleries',[])))")
  if [[ ${#IDS[@]} -eq 0 ]]; then
    echo "No published galleries from $MISE_URL"
    exit 0
  fi
  for gid in "${IDS[@]}"; do
    sync_gallery "$gid"
  done
else
  for gid in "$@"; do
    sync_gallery "$gid"
  done
fi

echo "==> Done. LOCAL_ROOT=$LOCAL_ROOT"