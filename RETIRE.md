# Retiring Argus — statelessness audit & decommission runbook

Mise now owns the authority for the photography suite (galleries, assets, the
per-photo signals, run status, and the review surface). Argus's job is narrowing
to a **stateless vision worker**: take images, produce reproducible vision
outputs, report them to Mise. This document is the audit of what state Argus still
holds, what is safe to turn off, and **exactly how to roll back** if the local
vision cutover needs to fall back to Argus.

> **Scope of the accompanying PR:** docs (this file) + removal of dead
> standalone-SaaS deploy/ops artifacts only. **No runtime behavior changes.** The
> code-level strip (SaaS/UI modules) is listed below as a gated follow-up.

---

## 1. What Mise now owns (source of truth)

| Authority | Owned by Mise | Argus's relationship |
|---|---|---|
| Galleries & assets | Mise | Argus only receives a `gallery_id` + a media path; it never owns gallery/asset records. |
| Per-photo signals (keywords, alt text, keeper/hero) | Mise (via the writeback it applies from Argus's structured callback) | Argus *produces* them; Mise *stores* them as truth. |
| Run status (`queued`/`done`/`error`) | Mise (recorded from the callback `status`) | Argus's job/run status is a transient operational mirror. |
| AI cost ledger (`ai_runs`, `/admin/ai-cost`) | Mise (from callback `cost_usd`/`latency_ms`) | Argus reports per-run cost; it is not the ledger of record. |
| Client / gallery review surface | Mise admin | Argus's `/ui/*` review pages are now duplicative. |
| Clients & their preferences | Mise (the suite's client records) | Argus stores a *tuning cache* of style/keyword prefs (see §3). |

## 2. Argus state inventory (what's authoritative vs cache)

| Store | Class | Keep? |
|---|---|---|
| `analysis_runs`, `photo_analyses` | **Reproducible output** — re-analyzing a gallery regenerates them; Mise's writeback is the truth | Keep as a cache; safe to purge/shorten retention |
| `mise_analyze_ledger` | **Run cache** — one active result per (gallery, client, folder-fingerprint); the dedup/idempotency anchor | Keep — this *is* the run cache |
| `callback_outbox` | **Operational** — dead-lettered callbacks for durable re-delivery | Keep |
| `jobs` | **Operational** — async analyze queue | Keep |
| `preferences` | **Client data (the one real duplication)** — per-client style/keyword tuning | Reduce to Mise-supplied (see §3); not changed in this PR |
| `tenants`, `tenant_api_keys`, `tenant_usage` | **Standalone-SaaS state** — multi-tenant registry/usage | Off by default; remove with the SaaS strip |
| `audit_log`, `stripe_webhook_events`, `cap_alert_log` | **Standalone-SaaS state** — audit/billing/cap alerts | Off by default; remove with the SaaS strip |

**Conclusion:** Argus already keeps no authoritative *business* state in the
Mise-integrated (studio, `ARGUS_SAAS_MODE=false`) deployment. Runs/photos are a
reproducible cache, `mise_analyze_ledger` is the run cache, and the only genuine
"client data" Argus owns is the `preferences` tuning cache (§3). Everything else
authoritative is the SaaS stack, which is dormant in studio mode.

## 3. The one reduction that changes runtime (follow-up, not this PR)

`preferences` (per-client `style` / `keyword_boosts` / `culling_bias`) is the only
business state Argus reads at analyze time that Mise could instead supply
per-request. Mise already passes `style` on the analyze call; the rest can follow.
Retiring the store (accept prefs from Mise, stop persisting) is a deliberate
runtime change and is **out of scope for the no-behavior-change cleanup** — it is
the first item of the gated follow-up.

## 4. What is safe to turn off

All of these are already off in the studio deployment (`ARGUS_SAAS_MODE=false`) —
this just records the levers:

| Capability | Turn off via | Notes |
|---|---|---|
| SaaS multi-tenant (signup, tenant API keys, usage caps) | `ARGUS_SAAS_MODE=false` (default) | Whole `/saas/*`, `/tenant/*`, `/admin/tenants/*` surface goes dormant |
| Stripe billing | leave `STRIPE_SECRET_KEY` unset | `/webhooks/stripe` inert |
| Rate limiting | `ARGUS_RATE_LIMIT_ENABLED=false` | Per-tenant/IP limits are a SaaS concern |
| Object storage (S3) | `ARGUS_STORAGE_BACKEND=local` (default) | |
| Cap alert emails/webhooks | leave `ARGUS_CAP_*`/`ARGUS_SMTP_*` unset | (the callback dead-letter alert reuses `ARGUS_CAP_WEBHOOK_URL` if set) |
| xAI daily budget guard | `ARGUS_XAI_DAILY_BUDGET_USD=0` | Irrelevant under local Qwen (`cost_usd=0`) |
| Standalone review UI | n/a (Mise is the review surface) | `/ui/*` pages duplicate Mise admin |

## 5. What Argus must keep (the contract Mise depends on)

Do **not** remove these while Argus is a fallback provider:

- **Analyze API:** `POST /analyze`, `POST /analyze-folder` (with `mise_gallery_id`,
  `correlation_id`, `callback_url`).
- **Reproducible outputs:** `GET /runs/{id}/export`, `/runs/{id}/structured`,
  `/runs/{id}/manifest.json`, `/runs/compare*`.
- **The structured callback** to Mise (`mise_client`, `structured_output`) — the
  hardened contract (idempotency key, correlation, status, retry → dead-letter →
  re-deliver, 401 re-auth). See [`docs/CALLBACK-CONTRACT.md`](docs/CALLBACK-CONTRACT.md).
- **Provider switch + parity harness** (`ARGUS_VISION_PROVIDER`, `provider_compare`)
  — these *are* the cutover/rollback mechanism. See
  [`docs/VISION-PROVIDERS.md`](docs/VISION-PROVIDERS.md).
- **Ops:** `/healthz`, `/vision/status`, `/metrics`, `/admin/callbacks/*`.
- **Run cache:** `mise_analyze_ledger`, `callback_outbox`, the `jobs` queue.

## 6. Exact rollback (fall back to Argus)

The cutover is reversible by configuration only — keep these valid throughout
decommission:

1. **Mise side:** set `MISE_VISION_PROVIDER=argus` (point Mise's vision back at
   Argus). This is the single switch.
2. **Argus reachable:** the Argus service URL Mise calls must resolve and serve
   (`/healthz` green).
3. **Inbound auth valid:** Mise's `MISE_ARGUS_TOKEN` must equal Argus's
   `ARGUS_API_TOKEN` (the bearer Argus requires on `/analyze*`). See
   [`docs/TOKEN-ROTATION.md`](docs/TOKEN-ROTATION.md).
4. **Outbound callback valid:** Argus's `ARGUS_MISE_URL` + `ARGUS_MISE_API_TOKEN`
   must still point at Mise's `/api/argus/callback` with a valid token (the 401
   re-auth self-heals a rotation, but the token must exist).

If all four hold, flipping `MISE_VISION_PROVIDER=argus` restores the previous
behavior with no Argus redeploy. Verify with the parity harness
(`scripts/compare_providers.py`) before and after.

## 7. Decommission phases

- **Phase A — this PR (no behavior change):** this audit + remove dead
  standalone-SaaS deploy/ops artifacts (`saas-bootstrap.sh`, `start-argus-saas.sh`,
  `fetch_saas_assets.py`, `wire-plutus-saas.sh`, `.env.saas.example`; the SaaS half
  of `deploy-smoke.sh`).
- **Phase B — gated follow-up (removes surfaces + tests):** strip the SaaS/UI code
  — `saas.py`, `billing.py`, `tenants.py`, `metering.py`, `rate_limit.py`,
  `cap_alerts.py`, `storage.py`, `audit.py`, `/ui/*` routes + `templates/`,
  `/webhooks/stripe`, `stripe_setup.py`/`stripe_smoke.py`, and the SaaS DB tables —
  with their tests. This removes live endpoints and is a deliberate surface change.
- **Phase C — `preferences` reduction (§3):** accept prefs from Mise per-request;
  drop the store.
- **Phase D — optional:** make run persistence ephemeral / shorten retention so
  Argus holds only the active run cache.

Each phase is independently reversible until Mise's local-vision path is proven on
real galleries via the parity gate.
