# argus
Photography-tuned vision & metadata API — auto-keywording, IPTC, alt text, culling signals.

Named for Argus (the many-eyed giant).

See [`docs/PHASE-0.md`](docs/PHASE-0.md) for the initial scope and what we're proving first.

**Vision:** xAI Grok API by default (`ARGUS_VISION_BACKEND=grok`). Mock for CI.
See [`docs/DOGFOOD-STANDARD.md`](docs/DOGFOOD-STANDARD.md). CI stays mock-only.

**Vision provider (reversible cutover):** `ARGUS_VISION_PROVIDER=grok|qwen` (default `grok`).
`qwen` routes the real path to a local Qwen3-VL (32B) on an OpenAI-compatible endpoint (Ollama)
emitting the *identical* structured output with `cost_usd:0`. Switching is a single env change;
Grok stays the default and instant rollback. See [`docs/VISION-PROVIDERS.md`](docs/VISION-PROVIDERS.md).

**Structured-output mode (Mise vision cutover):** set `ARGUS_STRUCTURED_OUTPUT=true`
to additionally emit the shared [`schemas/vision.schema.json`](schemas/vision.schema.json)
shape + `cost_usd`/`latency_ms` to Mise's `/api/argus/callback` on Mise-gallery runs —
apples-to-apples with the Qwen3-VL challenger. Off by default; the live Grok path is
unchanged. See [`docs/STRUCTURED-OUTPUT.md`](docs/STRUCTURED-OUTPUT.md).

**Studio mode (default):** homelab `:8010` vision for [Mise](https://github.com/Ayyitskevin/mise) gallery publish → [Plutus](https://github.com/Ayyitskevin/plutus) bundles. No Stripe, no SaaS signup.

This is the shared vision/metadata layer for the Kevin Lee photography suite (Mise → Argus → Plutus).

## Quickstart (Phase 0 — local dogfood)

```bash
cd argus
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: override model or data dir
export XAI_API_KEY=xai-...
export ARGUS_VISION_MODEL=grok-2-vision-1212
export ARGUS_DATA_DIR=./data

python -m app.main
# or uvicorn app.main:app --reload --port 8010
```

Open http://127.0.0.1:8010

- Use the **local folder path** form with one of your real edited galleries.
- Or upload a single test shot.
- Results appear in the UI and are stored in `data/argus.db`.

See `docs/PHASE-0.md` for exact scope and success criteria.

## Lightroom export (Phase 8)

Install `plugins/lightroom/Argus.lrplugin` in Lightroom Classic — post-export hook calls
`docs/lightroom_export_stub.py` over tailnet and writes sidecars locally.
See [`plugins/lightroom/README.md`](plugins/lightroom/README.md).

Capture One uses the same Python stub via [`plugins/capture-one/argus_post_export.sh`](plugins/capture-one/argus_post_export.sh).

Async integrations: `from app.async_client import AsyncArgusClient`.

Review UI supports dark mode (toggle in header; respects `prefers-color-scheme`).

Fleet ops: [`docs/TOKEN-ROTATION.md`](docs/TOKEN-ROTATION.md) · nightly DB backup via `scripts/backup-db.sh` + `ops/argus-backup.timer`.
