# Argus → Mise callback contract

How Argus reports a completed analysis to Mise (`POST {ARGUS_MISE_URL}/api/argus/callback?gallery_id=<id>`).
Mise relies on the invariant **one stable result per (gallery, run), idempotent** —
a re-run, retry, or re-delivery must never create a duplicate or lose a result.
This document tracks the contract and which hardening has landed.

## Body (excerpt)

```json
{
  "schema": "vision.schema.json",
  "provider": "argus-grok",
  "gallery_id": 42,
  "run_id": 123,
  "idempotency_key": "argus-g42-r123",
  "status": "done",
  "correlation_id": "<echoed from Mise, when supplied>",
  "photos": [ ... ],
  "cost_usd": 0.0246,
  "latency_ms": 1820.4
}
```

## Idempotency  ✅ (implemented)

- Every callback carries a stable **`idempotency_key`** in the body **and** as an
  `Idempotency-Key` HTTP header. It is derived purely from the run identity:
  `argus-g{gallery_id}-r{run_id}`.
- **Stable across retries / re-deliveries** (same run → same key) and **across
  re-analyses of an unchanged gallery**: the `mise_analyze_ledger` run cache
  returns the *same* `run_id` for an unchanged gallery+fingerprint, so the key is
  identical. A *changed* gallery → new fingerprint → new run → new `run_id` → new
  key = a genuinely new logical result.
- **Both sides dedupe on it.** Mise no-ops a key it has already applied (no
  duplicate writeback). Argus keys its own dead-letter / re-delivery store on it,
  so re-delivering the same result never double-sends.

## Correlation  ✅ (implemented)

Mise sends `correlation_id` on the analyze request (`POST /analyze-folder` form
field). Argus stores it on the job (queued mode) and **echoes it back unchanged**
on the callback, so paired/shadow runs (e.g. Grok vs Qwen) link on Mise's side.

## Status semantics  ✅ (implemented)

`status` is always exactly one of **`queued` | `done` | `error`**, normalized
before send, so Mise records an accurate last state and an Argus-side failure is
recorded — never a silent gap and never a crash of Mise's publish path.

## Auth

Authenticated with the Mise service token (`ARGUS_MISE_API_TOKEN`,
`Authorization: Bearer …`), attached only to the `MISE_URL`-derived callback —
never to a tenant-supplied URL.

## Roadmap (follow-up PRs)

- **Reliable delivery (PR 2):** retry transient failures (network / 5xx / 429)
  with exponential backoff; on exhaustion, **dead-letter** the payload locally
  (`callback_outbox`) and surface it, then re-deliver — keyed on the idempotency
  key so re-delivery is safe. `404`/`410` (unknown/stale gallery) is a **no-op**,
  not an error. A completed analysis is never lost to a failed callback.
- **Auth robustness / 401 re-auth (PR 3):** on `401`, reload the Mise service
  token from the environment/`.env` (the token-drift case) and retry once; on a
  hard failure, dead-letter + alert (log/webhook/email) — never a silent drop.
