# Vision providers (reversible Grok ↔ Qwen cutover)

Argus's real (non-mock) vision path is a **configurable, reversible provider**:

- **`grok`** — xAI Grok cloud (the **default**, behavior unchanged, rollback target).
- **`qwen`** — local **Qwen3-VL (32B)** on an OpenAI-compatible endpoint (Ollama).

Both providers emit the **identical** structured contract from the structured-output
mode (`docs/STRUCTURED-OUTPUT.md`) —
`{"photos":[{"basename","keywords","alt_text","keeper_score","hero_potential"}]}` plus
`cost_usd`/`latency_ms` — so Mise's validation gate can compare them apples-to-apples
and fall back instantly.

## Switch / rollback (single env change)

```bash
# Cut over to local Qwen
ARGUS_VISION_PROVIDER=qwen
ARGUS_QWEN_BASE_URL=http://mickeybot:11434/v1   # OpenAI-compatible /chat/completions
ARGUS_QWEN_VISION_MODEL=qwen3-vl:32b

# Roll back to Grok (instant): unset, or
ARGUS_VISION_PROVIDER=grok
```

| Var | Default | Notes |
|---|---|---|
| `ARGUS_VISION_PROVIDER` | `grok` | `grok` \| `qwen`. Mock backend (CI) ignores this. |
| `ARGUS_QWEN_BASE_URL` | `http://mickeybot:11434/v1` | Trusted local/tailnet endpoint only. |
| `ARGUS_QWEN_VISION_MODEL` | `qwen3-vl:32b` | |
| `ARGUS_QWEN_API_KEY` | unset | Optional bearer (usually none for Ollama). |
| `ARGUS_QWEN_TIMEOUT` | `180` | Seconds. |
| `ARGUS_QWEN_MAX_IMAGE_PX` | `1024` | Longest edge of the downsized derivative sent. |

`ARGUS_VISION_BACKEND` keeps its existing meaning (`mock` for CI/dev vs `grok` for real);
the provider selector chooses *which* real provider runs.

## Guarantees

- **Grok unchanged.** With `ARGUS_VISION_PROVIDER=grok` (default) the Grok code path,
  output, callback, and xAI budget/health behavior are byte-identical to before — proven
  by the existing suite, which runs at this default.
- **Same contract.** The Qwen path reuses the same `_build_result` normalizer as every
  other provider, so the downstream payload (callback shape, writeback, idempotency) is
  unchanged. `cost_usd` is real for Grok, **`0` for local Qwen**.
- **Idempotent.** One stable result per (gallery, run) regardless of provider.

## Qwen call mechanics

`POST {ARGUS_QWEN_BASE_URL}/chat/completions` with the photo as a base64 data URL and the
same structured-output prompt, `temperature: 0` + `response_format: {"type":"json_object"}`
for deterministic, schema-valid replies. The reply is parsed **strictly** — a malformed or
empty body is recorded as a provider/invalid-response error (`analysis_failed`), never
written half-parsed.

## Privacy

- Sends a **downsized web derivative** (`ARGUS_QWEN_MAX_IMAGE_PX` longest edge), never the
  original, and one image per request (structural cap).
- The Qwen endpoint is treated as **operator-trusted/local** (same trust model as
  `ARGUS_MISE_URL`). Do not point it at an unapproved cloud host — client media must stay
  on the approved local/tailnet model.

## Resilience

A provider failure on either path (HTTP error, timeout, unreachable endpoint, malformed
reply) is **swallowed and recorded as a failed analysis** — it never crashes the
analyze/callback flow.

## CI

`tests/test_vision_provider_qwen.py` exercises the Qwen path with a **mocked**
OpenAI-compatible endpoint (no live model): contract parity with Grok, `cost_usd:0`,
downsized-derivative check, strict-parse rejection, unreachable/HTTP-error resilience, and
that the default stays `grok` / mock ignores the switch. No live model calls in CI.
