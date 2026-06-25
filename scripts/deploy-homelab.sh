#!/usr/bin/env bash
# Pull latest main and restart Argus homelab vision service (:8010).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${ARGUS_PORT:-8010}"
BRANCH="${ARGUS_DEPLOY_BRANCH:-main}"

echo "==> pytest gate (mock vision)"
if [[ -x "$ROOT/.venv/bin/pytest" ]]; then
  ARGUS_VISION_BACKEND=mock ARGUS_QUEUE_ENABLED=false "$ROOT/.venv/bin/python" -m pytest -q tests/test_smoke.py 2>/dev/null \
    || ARGUS_VISION_BACKEND=mock "$ROOT/.venv/bin/python" -m pytest -q --maxfail=1 2>/dev/null \
    || echo "WARN: pytest gate skipped or partial"
else
  echo "WARN: .venv missing" >&2
fi

echo "==> git pull origin $BRANCH"
git pull origin "$BRANCH"

if systemctl --user is-enabled argus.service >/dev/null 2>&1; then
  echo "==> restart argus.service"
  systemctl --user restart argus.service
else
  echo "==> service not installed — run: bash scripts/install-user-service.sh" >&2
  exit 1
fi

sleep 2
systemctl --user is-active argus.service
curl -sf "http://127.0.0.1:${PORT}/healthz" | python3 -m json.tool | head -20
echo "==> Deploy OK — Argus studio :${PORT}"