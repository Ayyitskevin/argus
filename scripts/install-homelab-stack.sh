#!/usr/bin/env bash
# Homelab stack: Argus :8010 + Plutus :8030 as user systemd services
set -euo pipefail

ARGUS_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLUTUS_ROOT="${PLUTUS_ROOT:-$HOME/ai-workspace/plutus}"

echo "==> Installing homelab stack"
echo "    Argus:  $ARGUS_ROOT"
echo "    Plutus: $PLUTUS_ROOT"

bash "$ARGUS_ROOT/scripts/install-user-service.sh"
bash "$PLUTUS_ROOT/scripts/install-homelab-service.sh"

echo ""
echo "==> Homelab stack"
echo "    Pipeline:  http://127.0.0.1:8010/ui/pipeline"
echo "    Plutus:      http://127.0.0.1:8030/"
echo "    Argus logs:  journalctl --user -u argus -f"
echo "    Plutus logs: journalctl --user -u plutus-homelab -f"