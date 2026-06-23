# Argus Phase 5 — Complete (2026-06-23)

## Goal

Ship on mickey, prove real qwen3-vl output quality, keep mock CI safe.

## Slice 1 — Deploy hygiene

- [x] `argus-claude` → `~/ai-workspace/argus` rsync (excludes `.git`, `.venv`, `data`)
- [x] `.env` from `.env.example`; `app/config.py` loads dotenv
- [x] `argus.service` + `install-service.sh` (R22 DB backup)
- [x] Interim: `scripts/start-argus.sh` when sudo unavailable
- [x] Uvicorn on `0.0.0.0:8010` — `/healthz` green (`backend: mock` for service default)
- [ ] **Kevin sudo:** `bash install-service.sh` for persistent systemd

## Slice 2 — Real vision dogfood

### Synthetic / Grok (5/5 pass, 0% degenerate)

| Set | Count | Result |
|-----|-------|--------|
| `mnemosyne/scratch/fnb_gallery` | 2 | Pass after parser fix |
| `data/dogfood-gallery-grok` | 3 | Pass first try |

~3.5 min/image on mickey `qwen3-vl:32b`.

### Real delivery gallery (mise backup)

**Path:** `~/backups/mise/media/1/original/` (mise gallery 1 mirror from flow)

| Image | Keeper | Verdict |
|-------|--------|---------|
| 3× placeholder JPEGs | 0.0–0.1 | **Correct** — model flags abstract gradients as non-photographic (design mockup gallery, not camera work) |

0% degenerate JSON; parser + retry path stable after vision hardening.

### Real food photography (demo assets)

**Path:** `~/ai-workspace/argus/data/demo/` (`01-appetite.jpg`, `02-appetite.jpg` — cropped from live mise F&B UI)

Run:

```bash
cd ~/ai-workspace/argus
ARGUS_VISION_BACKEND=real ARGUS_DATA_DIR=./data/phase5-demo-real \
  .venv/bin/python scripts/dogfood_real.py data/demo --limit 2 --client-id kevin-demo-real
```

Log: `data/phase5-demo-real.log`

## Slice 3 — Vision hardening (shipped)

- `_extract_json_blob()` + `_ollama_json_content()` — thinking-field + prose JSON extraction
- Retry on empty/invalid/degenerate JSON
- `scripts/dogfood_real.py` — degenerate rate reporting, `--recursive`, `--data-dir`

## Definition of done

| Criterion | Status |
|-----------|--------|
| Keywords/culling useful on real food photos | **Demo run** (see log); mise gallery 1 is placeholders only on this mirror |
| `/healthz` green on mickey | **Yes** (`http://127.0.0.1:8010/healthz`, tailnet via `0.0.0.0`) |
| Zero model loads in mock CI | **Yes** (40 pytest) |
| <10% degenerate on 5+ images | **Yes** (8/8 cumulative, 0% degenerate) |
| Persistent systemd | **Blocked on sudo** — use `scripts/start-argus.sh` |

## Ops quick reference

```bash
# Deploy sync
rsync -av --exclude '.git' --exclude '.venv' --exclude 'data' \
  ~/ai-workspace/argus-claude/ ~/ai-workspace/argus/

# Start (no sudo)
~/ai-workspace/argus/scripts/start-argus.sh

# Real vision dogfood (human-gated — never in CI)
ARGUS_VISION_BACKEND=real python scripts/dogfood_real.py /path/to/gallery --limit 5

# Flip service back to mock after session
# edit .env: ARGUS_VISION_BACKEND=mock && restart uvicorn
```

## Next

Phase 5 is **done enough to proceed** (Phases 7–9 shipped). Optional: dogfood a full Kevin Lightroom export folder when available on mickey; `bash install-service.sh` when sudo is handy.