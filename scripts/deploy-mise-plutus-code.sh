#!/usr/bin/env bash
# Sync Plutus integration from mise-claude → flow /opt/mise (code + migration).
set -euo pipefail

SRC="${MISE_SRC:-$HOME/ai-workspace/mise-claude}"
FLOW_HOST="${MISE_FLOW_HOST:-flow}"
DEST="${MISE_DEST:-/opt/mise}"

if [[ ! -d "$SRC" ]]; then
  echo "Missing $SRC" >&2
  exit 1
fi

echo "==> Sync Plutus integration $SRC → $FLOW_HOST:$DEST"

rsync -av \
  "$SRC/app/plutus_recommend.py" \
  "$FLOW_HOST:$DEST/app/"

for f in argus_analyze.py jobs.py config.py service_api.py admin/galleries.py; do
  rsync -av "$SRC/app/$f" "$FLOW_HOST:$DEST/app/$f"
done

rsync -av "$SRC/templates/admin/gallery.html" "$FLOW_HOST:$DEST/templates/admin/"
rsync -av "$SRC/migrations/058_plutus_upsell.sql" "$FLOW_HOST:$DEST/migrations/"
rsync -av "$SRC/tests/test_smoke_plutus.py" "$FLOW_HOST:$DEST/tests/" 2>/dev/null || true

echo "==> Apply migration + restart on flow"
ssh "$FLOW_HOST" "sudo bash -s" <<'REMOTE'
set -euo pipefail
cd /opt/mise
if [[ -d .venv ]]; then
  source .venv/bin/activate
fi
python -c "
from app import db
db.migrate()
print('migrations applied')
"
systemctl restart mise
sleep 2
systemctl is-active mise
REMOTE

echo ""
echo "If migrate failed (mise.db owned by mise user), run on flow:"
echo "  ssh -t flow 'cd /opt/mise && sudo -u mise .venv/bin/python -c \"from app import db; db.migrate()\" && sudo systemctl restart mise'"
echo "==> Code synced. After migrate + restart, gallery admin shows Plutus tiles."