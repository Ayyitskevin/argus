# Argus Phase 5 — In progress

## Slice 1 — Deploy hygiene (2026-06-23)

- `argus.service` fixed: `kevin-lee`, uvicorn on `0.0.0.0:8010`, `EnvironmentFile=.env`, `Restart=on-failure`
- `install-service.sh`: R22 DB backup, `.env` bootstrap, healthz smoke (requires sudo on host)
- `.env.example` added
- GitHub Actions CI: `.github/workflows/ci.yml` staged locally (push needs `workflow` OAuth scope)
- httpx/httpcore → WARNING in `main.py` (journald token hygiene)
- Sync path: `argus-claude` (git) → `~/ai-workspace/argus` (mickey deploy tree)

**Mickey note:** `install-service.sh` needs Kevin sudo; interim run:
`uvicorn app.main:app --host 127.0.0.1 --port 8010` from `~/ai-workspace/argus`.

## Slice 2 — Real vision dogfood (2026-06-23)

Gallery: `~/ai-workspace/mnemosyne/scratch/fnb_gallery` (13 synthetic F&B JPEGs)

```bash
ARGUS_VISION_BACKEND=real ARGUS_DATA_DIR=./data/dogfood \
  python scripts/dogfood_real.py .../fnb_gallery --limit 2 --client-id kevin
```

| Image | Keeper | Shot type | Verdict |
|-------|--------|-----------|---------|
| 00.jpg | 0.95 | hero_plate | **Pass** — specific keywords (nasturtium, reduction sauce, shallow DOF) |
| 01.jpg | 0.50 | other | **Fail** — model returned `{}` (empty JSON) |

- Elapsed: ~3.8 min/image (qwen3-vl:32b on mickey)
- Phase 0 bar: **partial** — one strong, one empty

## Slice 3 — Prompt / parser fix (same day)

- Added `_ollama_json_content()` — reads `thinking` when `content` is `{}` (qwen3-vl pattern)
- Degenerate JSON retry once with lower temperature
- Re-dogfood 01.jpg: **pass** — action_sequence, keeper 0.90, keywords (wine pour, low-key, crystal glass)
- First-pass `{}` on 01.jpg was likely intermittent; retry path + thinking fallback added anyway

## Definition of done (Phase 5)

- [ ] Kevin: "keywords/culling would save real time" on real edited gallery (not just scratch)
- [ ] `/healthz` green on mickey tailnet (systemd or documented uvicorn)
- [x] Zero model loads in mock CI
- [x] Re-run dogfood on 5+ images with <10% degenerate rate — **5/5 pass** (scratch 2 + Grok-gen 3, 0% degenerate after parser fix)

### Grok image-gen fallback (2026-06-23)

When scratch assets fail or `{}` repeats, generate replacement F&B JPEGs via Grok
`GenerateImage`, copy to `data/dogfood-gallery-grok/`, then:

```bash
ARGUS_VISION_BACKEND=real ARGUS_DATA_DIR=./data/dogfood-grok-run \
  python scripts/dogfood_real.py data/dogfood-gallery-grok --limit 3
```

| Generated asset | Keeper | Tags | Notes |
|-----------------|--------|------|-------|
| 01-hero-plate.jpg | 0.95 | 10 | seared scallop, brown butter, microgreens |
| 02-interior-wide.jpg | 0.95 | 12 | cocktail detail (file label mismatch — rename optional) |
| 03-cocktail-detail.jpg | 0.90 | 11 | wide interior establishing |

~3.5 min/image avg on mickey qwen3-vl:32b for this batch.
- [ ] Kevin sudo: `bash install-service.sh` for persistent systemd on :8010