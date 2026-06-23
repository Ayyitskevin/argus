#!/usr/bin/env bash
# Snapshot argus SQLite DB (safe for WAL mode via sqlite3 .backup).
# Install nightly timer: sudo cp ops/argus-backup.* /etc/systemd/system/ && sudo systemctl enable --now argus-backup.timer
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${ARGUS_DATA_DIR:-$ROOT/data}"
DB="${ARGUS_DB_PATH:-$DATA_DIR/argus.db}"
BACKUP_DIR="${ARGUS_BACKUP_DIR:-$DATA_DIR/backups}"
RETAIN="${ARGUS_BACKUP_RETAIN:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/argus-${STAMP}.db"

if [[ ! -f "$DB" ]]; then
  echo "Database not found: $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
if command -v sqlite3 >/dev/null; then
  sqlite3 "$DB" ".backup '$OUT'"
else
  PYTHON="${ARGUS_PYTHON:-$ROOT/.venv/bin/python}"
  if [[ ! -x "$PYTHON" ]]; then
    PYTHON=python3
  fi
  "$PYTHON" - "$DB" "$OUT" <<'PY'
import shutil
import sqlite3
import sys
src, dest = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dest) as d:
    s.backup(d)
PY
fi
BYTES="$(wc -c < "$OUT" | tr -d ' ')"
echo "Backup written: $OUT (${BYTES} bytes)"

# Keep last N backups (newest first)
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/argus-*.db 2>/dev/null | tail -n +$((RETAIN + 1)) || true)
if ((${#OLD[@]})); then
  rm -f "${OLD[@]}"
  echo "Pruned ${#OLD[@]} old backup(s); retaining ${RETAIN}"
fi