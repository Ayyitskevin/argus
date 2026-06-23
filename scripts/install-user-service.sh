#!/usr/bin/env bash
# Install Argus as a user systemd service (no sudo). Survives logout if linger is on.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DEST="$UNIT_DIR/argus.service"
BACKUP_SVC="$UNIT_DIR/argus-backup.service"
BACKUP_TMR="$UNIT_DIR/argus-backup.timer"

echo "==> Argus user service from $ROOT"

if [[ ! -f "$ROOT/.venv/bin/uvicorn" ]]; then
  echo "Missing venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e ."
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "==> Created $ROOT/.env from .env.example"
fi

mkdir -p "$UNIT_DIR"
sed "s|%h|$HOME|g" "$ROOT/ops/argus-user.service" > "$DEST"
sed "s|%h|$HOME|g" "$ROOT/ops/argus-backup-user.service" > "$BACKUP_SVC"
cp "$ROOT/ops/argus-backup-user.timer" "$BACKUP_TMR"

# Stop ad-hoc uvicorn on :8010 so the unit can bind.
pkill -f 'uvicorn app.main:app --host 0.0.0.0 --port 8010' 2>/dev/null || true
sleep 1

systemctl --user daemon-reload
systemctl --user enable --now argus.service
systemctl --user enable --now argus-backup.timer

if loginctl show-user "$(whoami)" -p Linger 2>/dev/null | grep -q 'Linger=no'; then
  echo ""
  echo "NOTE: Linger is off — Argus stops when you log out."
  echo "  Enable once (needs sudo): sudo loginctl enable-linger $(whoami)"
fi

sleep 1
systemctl --user is-active argus.service
curl -sf "http://127.0.0.1:${ARGUS_PORT:-8010}/healthz" | head -c 200
echo
echo "==> User Argus running. Logs: journalctl --user -u argus -f"