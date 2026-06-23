#!/usr/bin/env bash
# Snapshot argus SQLite DB (safe for WAL mode via sqlite3 .backup).
set -euo pipefail

DATA_DIR="${ARGUS_DATA_DIR:-$HOME/ai-workspace/argus/data}"
DB="${ARGUS_DB_PATH:-$DATA_DIR/argus.db}"
BACKUP_DIR="${ARGUS_BACKUP_DIR:-$DATA_DIR/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/argus-${STAMP}.db"

if [[ ! -f "$DB" ]]; then
  echo "Database not found: $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
sqlite3 "$DB" ".backup '$OUT'"
echo "Backup written: $OUT"

# Keep last 14 backups
ls -1t "$BACKUP_DIR"/argus-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f