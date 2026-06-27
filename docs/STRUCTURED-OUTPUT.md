# Structured-output mode (Mise vision cutover)

Argus can emit the **same per-photo signals as the Qwen3-VL challenger** so the
Mise validation gate can compare them apples-to-apples and unblock the vision
cutover. This is an **additive, flag-gated** mode: with the flag off, the live
Grok export/callback path is byte-for-byte unchanged.

## The flag

```bash
ARGUS_STRUCTURED_OUTPUT=false        # default; live Grok path only
ARGUS_STRUCTURED_PROVIDER=argus-grok # provider label Mise pairs shadow rows on
```

When `ARGUS_STRUCTURED_OUTPUT=true`, a completed **Mise-gallery** analyze run
(one resolved via `mise_gallery_id`) additionally emits the shared contract to
Mise. Non-Mise runs and the existing Grok output are never affected.

## The contract

Per-photo shape (`schemas/vision.schema.json`, vendored from Mise's
`docs/WORKER-CONTRACT.md` + `schemas/vision.schema.json`):

```json
{ "photos": [ {
  "basename": "<exact file name>",
  "keywords": ["..."],
  "alt_text": "one line or null",
  "keeper_score": 0.0,
  "hero_potential": 0.0
} ] }
```

- `basename` is required (Mise matches it to gallery assets).
- `keeper_score` / `hero_potential` are floats in **[0,1] or null** — Argus
  clamps into range before sending (Mise rejects out-of-range deterministically).
- `alt_text` is a single line or null.

Argus flattens its internal `culling.keeper_score` / `culling.hero_potential`
onto the top level so its output validates against the same schema as Qwen.

## Callback

On run completion Argus POSTs to:

```
POST {ARGUS_MISE_URL}/api/argus/callback?gallery_id=<id>
Authorization: Bearer {ARGUS_MISE_API_TOKEN}
```

Body = the `photos` payload above **plus** the run-level fields Mise's `ai_runs`
ledger and `/admin/ai-cost` report consume:

```json
{
  "schema": "vision.schema.json",
  "provider": "argus-grok",
  "gallery_id": 42,
  "run_id": 123,
  "idempotency_key": "argus-g42-r123",
  "status": "done",
  "photos": [ ... ],
  "cost_usd": 0.0246,
  "latency_ms": 1820.4,
  "correlation_id": "<echoed from Mise>"
}
```

- `idempotency_key` is stable per (gallery, run) and also sent as an
  `Idempotency-Key` header, so Mise/Argus dedupe re-deliveries. `status` is always
  `queued|done|error`. See [`CALLBACK-CONTRACT.md`](CALLBACK-CONTRACT.md).
- `cost_usd` = sum of per-image spend (real Grok/cloud usage; simulated in mock/CI).
- `latency_ms` = sum of per-image inference time. **Summed, not wall-clock**, so it
  is deterministic and reproduces exactly on an idempotent re-emit.
- `correlation_id` is echoed only when Mise supplied one (so shadow pairs link).

The callback fires on a daemon thread (best-effort) and **no-ops when Mise is not
configured**, so CI/dev never makes an HTTP call. A callback failure never fails
an analyze that already succeeded.

### Passing the correlation id

Mise sends `correlation_id` on the analyze request (form field on
`POST /analyze-folder`). Argus stores it on the job (queued mode) and echoes it
back on the callback.

## Read-only preview endpoint

Mise's dry-run preview at `/admin/vision-cutover` (and the validation gate) can
pull a run without waiting for a callback:

```
GET /runs/{run_id}/structured                      -> { "photos": [...] }
GET /runs/{run_id}/structured?gallery_id=<id>      -> full callback body
GET /runs/{run_id}/structured?gallery_id=<id>&correlation_id=<cid>
```

Both are deterministic functions of the persisted run.

## Idempotency & statelessness

- The serializer is a **pure function of the persisted run** — the same `run_id`
  always re-emits the identical payload.
- Argus keeps only a **run cache** (the `mise_analyze_ledger`): one active
  queued/done entry per `(gallery, client, folder-fingerprint)`. A republish of an
  unchanged gallery returns the cached run instead of re-analyzing or re-firing.
- Mise owns gallery/asset state and dedups on `(gallery_id, run_id)` /
  `correlation_id`, so a retry cannot duplicate.

## Tests / CI

`tests/test_structured_output.py` validates Argus's output against
`schemas/vision.schema.json` with `jsonschema`, asserts score clamping and
flag-off no-op, and checks the callback wiring — all on the **mock** backend, no
network. CI stays mock-only.
