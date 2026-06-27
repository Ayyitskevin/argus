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

## Auth + 401 re-auth  ✅ (implemented)

Authenticated with the Mise service token (`ARGUS_MISE_API_TOKEN`,
`Authorization: Bearer …`), attached only to the `MISE_URL`-derived callback —
never to a tenant-supplied URL.

The token is a first-class part of the contract and **self-heals token drift**
(the known 401 outage class). On a `401`:

1. **Reload** `ARGUS_MISE_API_TOKEN` — fresh from the on-disk `.env` (an operator
   rotation the running process hasn't picked up yet), then the process env.
2. If the token **changed**, retry the delivery **once** with it — a rotated token
   recovers without a restart.
3. If it didn't change (or the retry still `401`s), **dead-letter + alert** with a
   clear `auth` status (`structured_log` `callback.auth_failure` + the operator
   webhook) — a completed run is never silently dropped, and it's re-deliverable.

Re-delivery (`/admin/callbacks/redeliver` + the worker tick) also reloads the
token before re-POSTing, so a token-drift dead-letter self-heals on the next pass.

## Reliable delivery  ✅ (implemented)

The structured callback delivers durably — a completed analysis is never lost to
a failed callback:

- **Retry with backoff.** Transient failures (network error, timeout, `5xx`,
  `429`) are retried with exponential backoff
  (`ARGUS_MISE_CALLBACK_MAX_ATTEMPTS`, default 3; `ARGUS_MISE_CALLBACK_BACKOFF_BASE`,
  default 0.5s) on the background delivery thread.
- **No-op on stale subject.** `404`/`410` (unknown/stale gallery) is a no-op, not
  an error — nothing to deliver, no retry, no dead-letter.
- **Dead-letter.** On exhaustion (or a hard `4xx` such as `401`), the payload is
  persisted to the `callback_outbox` table (keyed on `idempotency_key`, so a
  re-dead-letter of the same run updates one row, never duplicates) and surfaced
  via `structured_log` + an optional operator webhook (`ARGUS_CAP_WEBHOOK_URL`).
- **Re-delivery.** `POST /admin/callbacks/redeliver` (and a periodic worker tick)
  re-POSTs dead-lettered rows; success removes the row. The stable idempotency key
  makes re-delivery safe (Mise won't double-apply). `GET /admin/callbacks/dead-letters`
  lists what's pending (metadata only). Both require the admin token.

## Status

All three hardening slices are merged: idempotency key + correlation + status
(PR1), retry + dead-letter + re-delivery (PR2), and 401 re-auth + alerting (PR3).
The contract invariant — *one stable result per (gallery, run), idempotent, never
lost, never silently 401'd* — is now self-healing end to end.
