# argus — Phase 0 scope

**The one-line promise:** point argus at a folder (or single image) of your real edited photos and it returns rich, *photography-specific* structured analysis for each shot — precise keywords, actionable culling/keeper signals, ready-to-use alt text, and suggested IPTC metadata — accurate and useful enough that a working pro would keep or lightly edit the output rather than starting from zero.

Phase 0 is **not** a product or a full SaaS. It is the smallest thing that proves the specialized vision magic is real, built so *you* can dogfood it immediately on your own F&B and event galleries. Local only. No accounts, no billing, no cloud inference, no multi-tenant. If the outputs don't feel like a real photo editor's notes, we stop and fix the prompts/models before adding any plumbing.

---

## Why this is worth proving first

Every downstream AI tool in the suite (album design in mnemosyne, content generation in platekit, upsells in printlift, even packshot variants) needs high-signal understanding of the actual images.

Today a photographer (or an assistant) does this manually or with weak generic tools:
- Keywording for searchability, licensing, SEO, and client delivery
- Culling signals (which shots are keepers vs. near-misses)
- Alt text / descriptions for web galleries and accessibility
- IPTC/XMP population for professional handoff

Generic vision models are mediocre at this. A photography-tuned service that "sees" like a pro editor (composition, lighting quality, story value, technical issues, F&B-specific subject matter) is a real unfair advantage.

The "magic moment" in Phase 0 is opening the UI on one of your real galleries and thinking "these keywords and scores are actually good."

---

## Phase 0 scope — what's IN and what's OUT

**IN (build exactly this):**
- Accept a local folder path or individual image file(s) (your edited JPEGs/HEICs from real work).
- For each photo:
  - Basic technical metadata (width/height, orientation) via Pillow.
  - Rich analysis via a **vision model** (qwen3-vl:32b on the local fleet):
    - Domain-specific keywords/tags (composition, lighting, subject details, mood, story role).
    - Culling / quality signals (keeper score or ranked signals: focus/sharpness proxy, exposure issues, impact/aesthetic strength, "use this one" recommendation).
    - Short alt text (gallery/web ready).
    - Longer descriptive caption / notes.
    - Suggested IPTC fields (headline, caption, keywords list, source/credit).
- Persist results in a tiny local SQLite DB (per "run" + per photo).
- Simple web UI (FastAPI + Jinja + light HTMX):
  - Input a local path or upload for quick single shots.
  - Results view: image thumbnails + analysis cards.
  - Filter/sort by score, tags, or search keywords.
  - Export: full JSON for the run, or per-image sidecar JSON.
- Strong, photography-expert prompts that produce consistent, usable structured JSON (use Ollama `format: json`).
- Health and simple status endpoints so it can be called as a service later.

**OUT (explicitly do NOT build in Phase 0):**
- ❌ Writing IPTC/XMP *back into* the original image files (or even sidecars written to the source folder). Export only.
- ❌ User accounts, auth, multi-user, tenants.
- ❌ Any cloud inference or external API keys.
- ❌ Integration with mise DB or real galleries yet (Phase 0 is standalone dogfood tool).
- ❌ Batch processing of hundreds of images with progress UI or job queues (simple synchronous for now).
- ❌ Advanced editing of the analysis (Phase 0 proposes; you judge and copy-paste).
- ❌ PDF reports, Lightroom plugins, or fancy exports.
- ❌ Production packaging / systemd service (that comes after the magic is proven).

If you start wanting any of the OUT items, that's the signal Phase 0 succeeded.

---

## How it works — the assembly line (plain English)

Folder or image(s) → Ingest → Vision Look → Structure & Store → Review UI → Export

1. **Ingest** — List images (respect PHOTO_EXTS from mise patterns), read dimensions + basic exif transpose info with Pillow. Record original path (for local runs).
2. **Look** — For each image, send to the vision model with a carefully engineered prompt:
   - Persona: "expert food & beverage / event photographer and photo editor"
   - Task: describe composition, lighting, subject, technical quality, story value.
   - Force structured JSON output matching a clear schema.
   - The image is passed as base64 (or path if the client supports it).
3. **Structure** — Parse JSON, normalize scores/tags, compute simple aggregates if useful (e.g. suggested album hero candidates).
4. **Store** — SQLite: one `analysis_run` + many `photo_analysis` rows (path, dims, raw json + extracted fields for querying).
5. **Review** — Web UI shows the photos in a useful grid/list with their data. You can immediately see whether the culling signals and keywords would save real time.
6. **Export** — Download JSON bundle or individual .json sidecars.

This is deliberately the same four-station shape as mnemosyne Phase 0 (ingest / look / arrange / show) so the two tools compose naturally later.

---

## The data model (deliberately tiny)

Four-ish tables max:

- `analysis_run` — id, created_at, source_path (folder or label), model_used, summary_stats
- `photo_analysis` — run_id, image_path, width, height, format, 
  keywords (json or comma), culling_score (float 0-1 or detailed json), alt_text, description, suggested_iptc (json), raw_response (full model output for debugging)
- Simple indexes for filtering in the UI.

No more. We can always evolve; we must not over-engineer before the outputs are good.

---

## The AI calls — local, private, cheap to iterate

- Primary vision model: **qwen3-vl:32b** (the work vision model on the fleet per current roster).
- Fallback / alternate: the abliterated variant if needed for less censored outputs.
- Calls go direct to Ollama on localhost:11434 (or the local fleet address). Images stay on the machine.
- Prompt discipline: system prompt + detailed user prompt with examples + strict JSON schema. Temperature low for consistency.
- Every call is logged (model, latency, image identifier) so we can tune.
- Phase 0 deliberately avoids any paid cloud so you can iterate on 50–100 real images at zero marginal cost.

Later (Phase 2+), when this becomes a multi-tenant service, the same prompts + structured extraction will run on cloud inference and the cost is priced into the SaaS.

---

## Tech stack (use what you already know)

- Python + **FastAPI** (exact same as mise and the mnemosyne plan)
- **Jinja2** + minimal **HTMX** for the review UI (no heavy JS)
- **SQLite** (via the same db patterns as mise — see db.py style)
- **Pillow** (already vendored in mise imaging pipeline; reuse patterns for sRGB awareness if needed)
- `ollama` Python client or direct HTTP (see claude/botcore and mise patterns)
- Pydantic for request/response models and output validation
- Same testing/lint posture as mise (pytest units + smoke, ruff + tests)

No new frameworks. No frontend build step.

---

## Definition of done for Phase 0

You can run argus locally, point it at one of your real edited galleries (or a representative sample), and:

1. The UI loads and displays the images with analysis.
2. The keywords feel specific and professional (not "food on a plate").
3. Culling signals are directionally useful (high-scoring images are generally the stronger shots; it surfaces technical problems).
4. Alt text + IPTC suggestions are copy-paste ready or one small edit away.
5. You have the honest reaction: "This would actually save me (or an assistant) meaningful time on keywording and first-pass culling."

That reaction is the deliverable. Everything else is scaffolding to reach it.

---

## What comes after (roadmap, do not build yet)

- Phase 1: Better prompting + few-shot examples or fine-tuning signals, IPTC sidecar writing, simple API client for mise/mnemosyne to call over the tailnet.
- Phase 2: Service-ize it (Odysseus-style or standalone), expose over Tailscale, add queuing for larger batches, cloud inference option with cost accounting.
- Later: Lightroom/ Capture One plugin surface, direct mise gallery import, batch "analyze entire project" jobs, learned preferences per client or style.

---

## Open questions (answer before heavy scaffolding)

1. **Test data location**: Where is a good representative folder of edited F&B or event photos on disk right now that we can safely point Phase 0 at? (Absolute path preferred for local runs.)
2. **Output schema priorities**: Which fields matter most first? (e.g. top 8–12 keywords, a single 0–100 keeper score + 3–4 boolean flags, alt_text ≤ 125 chars, full IPTC block?)
3. **Model choice**: Stick with qwen3-vl:32b, or start with a lighter one for faster iteration then switch to the 32b?
4. **UI first or API first?** Do you want the web review UI immediately, or start with a CLI + JSON endpoint and add the pretty view second?
5. **Hosting for dogfood**: Run on mickey (access to best models) or elsewhere? Confirm it can read your photo folders (permissions).

Once these are settled we scaffold the minimal end-to-end loop (ingest → vision call → UI that makes the magic visible).

---

## Naming & conventions

- GitHub repo: `argus`
- Internal/product name: `photometa`
- Follow mise/mnemosyne conventions: local-first, explicit Phase boundaries, small data model, heavy prompt discipline, reuse existing patterns (Pillow, FastAPI shape, env config, ruff + tests).
- Update ORACLE + shared handoffs when reusable knowledge is produced.

Let's prove the vision layer is real.
