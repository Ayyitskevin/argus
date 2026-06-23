# Argus Phase 4 Plan

> Production polish on top of the Phase 3 fleet integration. All slices stay on
> `ARGUS_VISION_BACKEND=mock` unless Kevin explicitly gates real vision.

## Slices (ordered)

### Slice 1 — Cloud cost config (done in Phase 3 tail)
- `ARGUS_CLOUD_COST_PER_IMAGE` + `ARGUS_CLOUD_BACKEND=simulated|stub|disabled`
- `/jobs/costs` + `service.simulated_cloud_cost()`
- Exposed in `/healthz`

### Slice 2 — History-based preferences (this pass)
- `db.get_client_history_stats()` aggregates prior runs + photo analyses
- `service.load_preferences()` merges history when explicit prefs absent
- `vision._apply_prefs()` honors `shot_type_preference`
- `GET /clients/{client_id}/history`

### Slice 3 — Production hardening (this pass)
- Optional bearer auth via `ARGUS_API_TOKEN` (open when unset)
- In-process `/metrics` counters (uptime + analyze/job/photo totals)
- `/healthz` reports `auth_enabled`

### Slice 4 — Plugin / export surfaces (this pass)
- Restore `docs/lightroom_export_stub.py` as a runnable ArgusClient example
- CSV + XMP paths unchanged from Phase 3

### Slice 5 — Deferred
- Deeper mise direct-DB hooks (read-only gallery index)
- Prometheus exporter / structured logging sink
- Real cloud vision backend (human-gated, never default)

## Verification

```bash
cd ~/ai-workspace/argus-claude
ARGUS_VISION_BACKEND=mock .venv/bin/python -m pytest -q
```

All tests must pass with zero Ollama model loads.