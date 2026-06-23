# argus
Photography-tuned vision & metadata API — auto-keywording, IPTC, alt text, culling signals.

Named for Argus (the many-eyed giant).

See [`docs/PHASE-0.md`](docs/PHASE-0.md) for the initial scope and what we're proving first.

**Vision:** xAI Grok API only (`ARGUS_VISION_BACKEND=grok`). Mock for CI. No local qwen/Ollama.
See [`docs/DOGFOOD-STANDARD.md`](docs/DOGFOOD-STANDARD.md). CI stays mock-only.

This is the shared vision/metadata layer for the photography AI suite (feeds mnemosyne/albumwright, platekit, etc.).

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
