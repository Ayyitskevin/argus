#!/usr/bin/env bash
# Point homelab Argus at Plutus studio (:8030) — shared API token, no storefront tenant.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUTUS_ENV="${PLUTUS_ENV_FILE:-$ROOT/../plutus/.env.homelab}"
ARGUS_ENV="${ARGUS_ENV_FILE:-$ROOT/.env}"
PLUTUS_URL="${ARGUS_PLUTUS_URL:-http://127.0.0.1:8030}"

if [[ ! -f "$PLUTUS_ENV" ]]; then
  echo "Missing plutus env: $PLUTUS_ENV" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$PLUTUS_ENV"
set +a

API_TOKEN="${PLUTUS_API_TOKEN:-}"
if [[ -z "$API_TOKEN" ]]; then
  echo "PLUTUS_API_TOKEN required in $PLUTUS_ENV" >&2
  exit 1
fi

echo "==> Wire Argus → Plutus homelab ($PLUTUS_URL) → $ARGUS_ENV"
python3 - <<PY
from pathlib import Path

env_path = Path("${ARGUS_ENV}")
updates = {
    "ARGUS_PLUTUS_URL": "${PLUTUS_URL}",
    "ARGUS_PLUTUS_TOKEN": "${API_TOKEN}",
    "ARGUS_PLUTUS_TIMEOUT": "120",
}
lines = env_path.read_text().splitlines() if env_path.is_file() else []
out, seen = [], set()
for line in lines:
    if "=" in line and not line.strip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
        if key in {"ARGUS_PLUTUS_ADMIN_TOKEN", "ARGUS_PLUTUS_TENANT_ID"}:
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
env_path.write_text("\\n".join(out).rstrip() + "\\n")
for key in sorted(updates):
    if "TOKEN" in key:
        print(f"  {key}=***")
    else:
        print(f"  {key}={updates[key]}")
PY

if systemctl --user is-active argus >/dev/null 2>&1; then
  echo "==> restart argus"
  systemctl --user restart argus
  sleep 2
elif systemctl is-active argus >/dev/null 2>&1; then
  sudo systemctl restart argus
  sleep 2
fi

curl -sf "${PLUTUS_URL}/healthz" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('  plutus health:', d.get('status'), 'studio_mode=', d.get('studio_mode'))
"
echo "Done — Argus pipeline → Plutus studio :8030"