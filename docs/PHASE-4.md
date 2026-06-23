# Argus Phase 4 — Complete

## Shipped

1. **History-based preferences**
   - `GET /clients/{client_id}/history` returns run/photo counts, top shot type,
     keyword frequency, and average keeper/hero scores.
   - `service.load_preferences()` auto-fills `keyword_boosts`,
     `shot_type_preference`, and `culling_bias` from history when the client has
     no explicit prefs row.

2. **Production hardening**
   - Set `ARGUS_API_TOKEN` to require `Authorization: Bearer <token>` on POST
     analyze/import/preferences/sidecar routes. Unset = open (local default).
   - `GET /metrics` exposes in-process counters + uptime.
   - `/healthz` includes `auth_enabled`.

3. **Plugin example**
   - `docs/lightroom_export_stub.py` demonstrates remote analyze + local sidecar
     pull via `ArgusClient`.

## Mock-only verification

```bash
ARGUS_VISION_BACKEND=mock pytest -q
```

## Next (Phase 5 candidates)

- Mise gallery index read hook (no DB writes)
- Prometheus `/metrics` exporter option
- Human-gated real vision on mickey :8010