# Argus

**Photography-tuned vision and metadata service for working pros.**

Point Argus at a folder of edited JPEGs (or a single shot) and get structured,
editor-grade analysis back: precise keywords, keeper and hero scores, gallery-ready
alt text, captions, and suggested IPTC fields. Built for food & beverage, event,
and portrait workflows — not generic image tagging.

Named for Argus Panoptes, the many-eyed giant of Greek myth.

---

## What it is

Argus is a **FastAPI + SQLite** homelab service (`:8010`) that turns photos into
reusable metadata other tools can trust. It is the shared **vision brain** in the
[Kevin Lee Photography](https://kleephotography.com) software suite:

```text
Mise (galleries & CRM) → Argus (vision) → Plutus (print offers)
                      ↘ Mnemosyne (albums) · Dionysus (content) · Hestia (orchestration)
```

**Per image, Argus produces:**

| Output | Use |
|--------|-----|
| Keywords / tags | Search, licensing, SEO, client delivery |
| Keeper & hero scores | Culling and album shortlists |
| Alt text & description | Web galleries and accessibility |
| Suggested IPTC | Professional handoff and DAM workflows |
| Shot type & technical notes | Downstream content and layout tools |

Outputs are forced to a **structured JSON schema** so humans, APIs, and sibling
services can consume them without re-parsing prose.

**What Argus is not:** a gallery host, a billing system, or a replacement for
Lightroom/Capture One. It analyzes; you (or Mise) own the authoritative records.

---

## Who it's for

- **Solo photographers and small studios** dogfooding on real edited work — the
  Phase 0 bar is "would a working pro keep or lightly edit this output?"
- **Suite integrators** — [Mise](https://github.com/Ayyitskevin/mise),
  [Plutus](https://github.com/Ayyitskevin/plutus),
  [Mnemosyne](https://github.com/Ayyitskevin/mnemosyne), and
  [Hestia](https://github.com/Ayyitskevin/hestia) call Argus over HTTP instead of
  running their own vision stacks.
- **Operators on a homelab** — Tailscale-friendly, mock backend for CI, optional
  bearer auth, job queue, sidecar export, Prometheus metrics.

---

## How it works

```text
Folder or upload → ingest (Pillow / RAW preview) → vision model → SQLite → review UI / API export
                                                      ↓
                                            callback to Mise (optional)
```

1. **Ingest** — Walk a local path or accept uploads; support JPEG, HEIC, PNG, TIFF, and RAW via embedded preview.
2. **Look** — Send a downsized derivative to a vision provider with photography-expert prompts (F&B, events, portrait styles).
3. **Store** — Persist runs and per-photo analyses in `data/argus.db`; optional `.argus` / IPTC / XMP sidecars.
4. **Serve** — REST API, HTMX review UI, async job queue, CSV/JSON export, Mise gallery hooks, pipeline UI (Mise → Plutus).

Vision backends:

| Mode | Backend | When |
|------|---------|------|
| CI / dev | `mock` | Default — no API spend |
| Production | `grok` | xAI Grok vision (`XAI_API_KEY`) |
| Local cutover | `qwen` | Qwen3-VL 32B on Ollama — same contract, `cost_usd: 0` |

Switch providers with one env var; measure before cutover with the parity harness.
See [`docs/VISION-PROVIDERS.md`](docs/VISION-PROVIDERS.md).

---

## Suite role (studio mode)

Default deployment is **studio mode** on the homelab: vision for Mise gallery
publish flows and Plutus bundle generation. No Stripe, no public SaaS signup
(`ARGUS_SAAS_MODE=false`).

When Mise triggers a gallery analyze, Argus can POST structured results back to
Mise's callback endpoint (idempotent, with retry and dead-letter delivery).
Mise owns galleries, per-photo signals, run status, and the review surface;
Argus produces vision output and holds a reproducible cache.

**Evolution:** Argus is narrowing to a **stateless vision worker**. See
[`RETIRE.md`](RETIRE.md) for the state audit, what to turn off, and how to roll
back with `MISE_VISION_PROVIDER=argus`.

---

## Quickstart

```bash
git clone https://github.com/Ayyitskevin/argus.git
cd argus
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # mock backend — safe default

python -m app.main
# → http://127.0.0.1:8010
```

**Dogfood on real work:**

- Submit a **local folder path** to one of your edited galleries, or upload a single test shot.
- Review results in the UI; data lives in `data/argus.db`.

**Real vision (operator-gated):**

```bash
export XAI_API_KEY=xai-...
export ARGUS_VISION_BACKEND=grok
export ARGUS_VISION_MODEL=grok-4-fast
```

See [`docs/DOGFOOD-STANDARD.md`](docs/DOGFOOD-STANDARD.md) and [`docs/PHASE-0.md`](docs/PHASE-0.md).

---

## Integrations

| Surface | Path |
|---------|------|
| REST API | `POST /analyze`, `POST /analyze-folder`, `GET /runs/{id}/export` |
| Async jobs | `POST /jobs`, `GET /ui/jobs/{id}` |
| Mise | `POST /import/mise-project`, gallery pipeline UI, structured callback |
| Python client | `from app.async_client import AsyncArgusClient` |
| Lightroom Classic | `plugins/lightroom/Argus.lrplugin` |
| Capture One | `plugins/capture-one/argus_post_export.sh` |
| Ops | `/healthz`, `/vision/status`, `/metrics`, [`docs/TOKEN-ROTATION.md`](docs/TOKEN-ROTATION.md) |

---

## Documentation

| Doc | Topic |
|-----|-------|
| [`docs/PHASE-0.md`](docs/PHASE-0.md) | Original scope and success criteria |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phases 5–12 and fleet plan |
| [`docs/VISION-PROVIDERS.md`](docs/VISION-PROVIDERS.md) | Grok ↔ Qwen reversible cutover |
| [`docs/STRUCTURED-OUTPUT.md`](docs/STRUCTURED-OUTPUT.md) | Mise callback schema mode |
| [`docs/CALLBACK-CONTRACT.md`](docs/CALLBACK-CONTRACT.md) | Idempotency, correlation, delivery |
| [`RETIRE.md`](RETIRE.md) | Stateless worker audit and rollback |
| [`schemas/vision.schema.json`](schemas/vision.schema.json) | Shared vision payload shape |

---

## Stack

Python 3.11+ · FastAPI · Jinja2 / HTMX · SQLite · Pillow · httpx · Pydantic · pytest · ruff

Part of the KLP photography suite by [Kevin Lee](https://github.com/Ayyitskevin) —
insurance broker, photographer, and builder of self-hosted tools for real client work.