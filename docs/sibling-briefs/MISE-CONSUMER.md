# Mise consumer-side work-order — wire up the Argus vision contract

> **For a Mise-scoped session.** This is the producer-side spec: the Argus
> contract is already built and merged (PRs #3–#9); the contract details are
> inlined here so the Mise session needs no access to the Argus repo. Argus is the
> rollback provider — do **not** assume it's going away yet.

## Why this is the critical path

Argus already emits a hardened, idempotent, reversible vision contract, and serves
a parity endpoint to compare providers. **None of it is load-bearing until Mise
consumes it.** This work-order makes Mise the authority that uses Argus's signals,
records cost/status, drives the Grok→Qwen cutover behind a flag, and supplies the
client tuning that lets Argus become fully stateless.

---

## The Argus contract (inlined — what Mise integrates against)

Let `ARGUS_URL` = the Argus base URL, `ARGUS_TOKEN` = the shared bearer
(Mise's `MISE_ARGUS_TOKEN` **must equal** Argus's `ARGUS_API_TOKEN`).

### 1. Trigger analysis
```
POST {ARGUS_URL}/analyze-folder
Authorization: Bearer {ARGUS_TOKEN}
form fields: mise_gallery_id=<id>, correlation_id=<uuid>, client_id=<client>,
             limit=0, callback_url=<optional override>
```
Argus resolves the gallery's originals, analyzes, and POSTs the result back (below).
Re-requesting an unchanged gallery is deduped server-side (same `run_id`).

### 2. The callback Argus sends you
```
POST {MISE_BASE_URL}/api/argus/callback?gallery_id=<id>
Authorization: Bearer <argus's outbound token>
Idempotency-Key: argus-g<gallery_id>-r<run_id>
```
```json
{
  "schema": "vision.schema.json",
  "provider": "argus-grok",            // or argus-qwen — pair shadow runs on this
  "gallery_id": 42,
  "run_id": 123,
  "idempotency_key": "argus-g42-r123", // stable per (gallery, run); also a header
  "status": "done",                    // queued | done | error
  "correlation_id": "<echoed from your request>",
  "photos": [
    { "basename": "IMG_001.jpg", "keywords": ["..."],
      "alt_text": "one line or null",
      "keeper_score": 0.82,            // float [0,1] or null
      "hero_potential": 0.71 }         // float [0,1] or null
  ],
  "cost_usd": 0.0246,                  // real for Grok, 0 for local Qwen
  "latency_ms": 1820.4
}
```
Delivery is durable on Argus's side: it retries transient failures, dead-letters +
re-delivers on hard failure, and self-heals a rotated token on 401. **Your handler
must be idempotent** so re-deliveries don't double-apply.

### 3. Pull endpoints (for the dry-run / gate — no callback needed)
```
GET {ARGUS_URL}/runs/{run_id}/structured?gallery_id=<id>   -> the callback body above
GET {ARGUS_URL}/runs/compare/providers?a=<grokRunId>&b=<qwenRunId>
```
The compare report (read-only, deterministic) returns:
```json
{
  "providers": {"a": "grok", "b": "qwen"},
  "photo_counts": {"a": N, "b": N, "common": N, "only_a": N, "only_b": N},
  "cost_usd": {"a": 0.05, "b": 0.0, "delta": -0.05},
  "latency_ms": {"a": 1800, "b": 4200, "delta": 2400},
  "agreement": {
    "mean_keeper_abs_delta": 0.06, "mean_hero_abs_delta": 0.08,
    "keyword_jaccard_mean": 0.71, "shot_type_agree_rate": 0.84
  },
  "verdict": {"within_tolerance": true, "thresholds": {...}, "reasons": []},
  "per_photo": [ ... worst keeper divergence first ... ]
}
```

---

## What Mise must build

### R1 — Idempotent callback handler  *(unblocks #6–#8)*
- `POST /api/argus/callback?gallery_id=` — authenticate the bearer; **dedupe on
  `idempotency_key`** (and/or the `Idempotency-Key` header). One writeback per
  `(gallery_id, run_id)`; a re-delivery is a no-op that returns 2xx.
- Return `404`/`410` for an unknown/stale gallery (Argus treats that as a no-op,
  not an error — don't make it retry forever). Return `2xx` on success, `5xx` only
  for genuinely retryable failures.
- Validate `photos[]` against `vision.schema.json`; reject out-of-range scores.

### R2 — Status + cost ledger
- Record `status` (`queued|done|error`) as the gallery run's **last state** so the
  Mise dashboard is accurate; an Argus-side `error` is recorded, never crashes the
  publish path.
- Append `cost_usd` + `latency_ms` to the **`ai_runs` ledger**; surface in
  **`/admin/ai-cost`**. (Qwen is `0` — that's the point of the cutover.)

### R3 — Correlation / shadow pairing
- Send a `correlation_id` on the analyze request and **store it on the run**; the
  callback echoes it back. Use it to link a Grok shadow run and a Qwen shadow run
  of the same gallery.

### R4 — Provider flag + shadow orchestration
- `MISE_VISION_PROVIDER = argus | qwen` (and the live switch to flip/rollback).
- Shadow mode: run a gallery through **both** providers, store the pair (linked by
  `correlation_id`), and keep both results for comparison.

### R5 — `/admin/vision-cutover` gate
- Pull `GET /runs/compare/providers?a=&b=` for each shadow pair; show cost/latency
  deltas + score/keyword/shot-type agreement + the `within_tolerance` verdict.
- **Gate the flip** on the verdict across a sample of galleries.

### R6 — Own client preferences  *(unblocks Argus statelessness, Phase C)*
- Make Mise the source of truth for per-client `style` / keyword tuning, and pass
  it on the analyze request. Once Mise supplies prefs per-request, Argus can drop
  its local `preferences` store entirely.

### R7 — Writeback as truth
- Apply `photos[]` to gallery assets as the authoritative signals, matching by
  **`basename`**. This is the store of record Argus deliberately does not own.

---

## Acceptance criteria

- Re-delivering the same callback (same `idempotency_key`) produces **no duplicate
  writeback**; re-analyzing an unchanged gallery is the same logical result.
- `correlation_id` round-trips; Grok/Qwen shadow rows link and differ only by provider.
- `cost_usd`/`latency_ms` land in the `ai_runs` ledger and show in `/admin/ai-cost`.
- `/admin/vision-cutover` shows a real parity verdict and gates the flip.
- `MISE_VISION_PROVIDER=qwen` flips vision local; `=argus` rolls back instantly
  (see Argus `RETIRE.md §6` for the four conditions that keep rollback valid).
- A `404` for a stale gallery is a no-op; an Argus error is recorded, never crashes
  publish.

## Guardrails

- `claude/...` branches, small **draft PRs a human merges**; backward-compatible;
  independently green; mock the Argus endpoints in CI (no live Argus/model calls).
- **Do not retire or unreachable-ify Argus** until the Qwen path is proven on real
  galleries via the gate — Argus is the rollback.

## Kickoff prompt (paste into a Mise-scoped session)

> Wire Mise up to consume Argus's vision contract so Mise owns the signals, cost,
> status, and the cutover. Implement: (1) an **idempotent** `POST /api/argus/callback`
> handler that dedupes on `idempotency_key`, records `status` + `cost_usd`/`latency_ms`
> to the `ai_runs` ledger / `/admin/ai-cost`, echoes/stores `correlation_id`, and
> applies `photos[]` to gallery assets by `basename` (404 a stale gallery as a no-op);
> (2) a `MISE_VISION_PROVIDER=argus|qwen` flag with shadow orchestration that runs a
> gallery through both providers and links them by `correlation_id`; (3) an
> `/admin/vision-cutover` page that pulls Argus `GET /runs/compare/providers?a=&b=`
> and gates the flip on `verdict.within_tolerance`; (4) Mise as source of truth for
> per-client style/keyword prefs, passed on the analyze request. The Argus contract
> shape is fixed (see the brief). Propose a plan first, then implement on a `claude/`
> branch as small draft PRs. Keep Argus reachable as the rollback.
