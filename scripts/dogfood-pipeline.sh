#!/usr/bin/env bash
# Homelab pipeline dogfood — Mise gallery → vision → Plutus review + pitch (studio mode)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source .venv/bin/activate 2>/dev/null || true

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

GALLERY_ID="${1:-1}"
HOST="${ARGUS_HOST:-127.0.0.1}"
PORT="${ARGUS_PORT:-8010}"
BASE="http://${HOST}:${PORT}"
TOKEN="${ARGUS_API_TOKEN:?ARGUS_API_TOKEN required}"

echo "==> Argus + Mise + Plutus health"
curl -sf "$BASE/healthz" | python3 -c "
import json,sys
h=json.load(sys.stdin)
print('  argus:', h['status'])
m=h['checks'].get('mise',{})
p=h['checks'].get('plutus',{})
print('  mise:', m.get('status'), 'reachable=', m.get('reachable'))
print('  plutus:', p.get('status'), 'reachable=', p.get('reachable'))
assert m.get('reachable'), 'Mise unreachable — check flow:8400 / ARGUS_MISE_URL'
assert p.get('reachable'), 'Plutus unreachable — start plutus-homelab :8030'
"

echo "==> Pipeline run-all gallery #${GALLERY_ID}"
HEADERS=$(mktemp)
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "$BASE/ui/pipeline/run-all/${GALLERY_ID}" \
  -d "api_token=${TOKEN}" -D "$HEADERS")
LOC=$(grep -i '^location:' "$HEADERS" | awk '{print $2}' | tr -d '\r' || true)
rm -f "$HEADERS"
if [[ "$CODE" != "303" ]]; then
  echo "run-all failed HTTP $CODE" >&2
  exit 1
fi
echo "  redirect=$LOC"
python3 - <<PY
from urllib.parse import parse_qs, urlparse, unquote_plus
import urllib.request

loc = "${LOC}"
qs = parse_qs(urlparse(loc).query)
msg = unquote_plus((qs.get("msg") or [""])[0])
review = unquote_plus((qs.get("review_url") or [""])[0])
pitch = unquote_plus((qs.get("pitch_url") or [""])[0])
print("  steps:", msg)
print("  review:", review or "(none)")
print("  pitch:", pitch or "(none)")
if "error" in qs:
    raise SystemExit(unquote_plus(qs["error"][0]))
if not review or not pitch:
    raise SystemExit("pipeline did not return review_url and pitch_url")

with urllib.request.urlopen(review, timeout=30) as resp:
    body = resp.read()
    assert resp.status == 200, resp.status
    assert b"Upsell bundles" in body or b"bundle" in body.lower(), "review page missing bundles"

with urllib.request.urlopen(pitch, timeout=30) as resp:
    text = resp.read().decode()
    assert resp.status == 200, resp.status
    assert len(text.strip()) > 20, "pitch.txt too short"
PY

echo "==> Pipeline dogfood OK"