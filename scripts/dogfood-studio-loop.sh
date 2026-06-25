#!/usr/bin/env bash
# Studio loop: Mise gallery → Argus vision → Plutus bundles (review + pitch, no storefront).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

for ENV_FILE in \
  "${ARGUS_ENV_FILE:-$ROOT/.env}" \
  "${PLUTUS_ENV_FILE:-$ROOT/../plutus/.env.homelab}"
do
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
done

GALLERY_ID="${1:-${MISE_GALLERY_ID:-1}}"
LIMIT="${ARGUS_DOGFOOD_LIMIT:-2}"
TOKEN="${ARGUS_API_TOKEN:-}"
ARGUS_BASE="${ARGUS_PUBLIC_URL:-http://127.0.0.1:${ARGUS_PORT:-8010}}"
PLUTUS_BASE="${ARGUS_PLUTUS_URL:-http://127.0.0.1:8030}"

if [[ -z "$TOKEN" ]]; then
  echo "ARGUS_API_TOKEN required" >&2
  exit 1
fi

echo "==> Studio loop gallery ${GALLERY_ID} (limit ${LIMIT})"

echo "==> Argus pipeline run-all"
LOC=$(curl -sf -o /dev/null -w '%{redirect_url}' -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -d "api_token=${TOKEN}" \
  "${ARGUS_BASE}/ui/pipeline/run-all/${GALLERY_ID}") || {
    echo "FAIL: run-all unreachable at ${ARGUS_BASE}" >&2
    exit 2
  }

if [[ "$LOC" == *error=* ]]; then
  echo "FAIL: ${LOC}" >&2
  exit 2
fi

REVIEW=$(python3 -c "import sys, urllib.parse as u; q=u.parse_qs(u.urlparse(sys.argv[1]).query); print((q.get('review_url') or [''])[0])" "$LOC")
PITCH=$(python3 -c "import sys, urllib.parse as u; q=u.parse_qs(u.urlparse(sys.argv[1]).query); print((q.get('pitch_url') or [''])[0])" "$LOC")

if [[ -z "$REVIEW" || -z "$PITCH" ]]; then
  echo "FAIL: missing review_url or pitch_url in redirect: ${LOC}" >&2
  exit 2
fi

echo "  review: ${REVIEW}"
echo "  pitch:  ${PITCH}"

curl -sf "${REVIEW}" >/dev/null || { echo "FAIL: review page ${REVIEW}" >&2; exit 2; }
curl -sf "${PITCH}" | head -c 40 | grep -q . || { echo "FAIL: pitch empty ${PITCH}" >&2; exit 2; }

curl -sf "${PLUTUS_BASE}/healthz" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert d.get('studio_mode') is True, d
print('  plutus studio_mode ok')
"

echo "==> Studio loop PASSED"