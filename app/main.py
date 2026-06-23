"""Argus / photometa — minimal FastAPI for Phase 0 vision & metadata analysis.

Local-first. Drop a folder of real photos, get structured photography-grade output.
"""

import csv
import io
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from . import config, db, vision
from .vision import AnalysisResult, Culling, make_thumbnail  # for typed responses (C)


def write_sidecar(image_path: str, analysis_data: dict, sidecar_dir: str | None = None) -> dict[str, Path]:
    """Write sidecar(s) (Phase 1 feature).
    - Always writes <basename>.argus.json with full argus analysis.
    - If suggested_iptc present, also writes <basename>.iptc.json (clean IPTC fields).
    - Phase 3 slice 5: also writes <basename>.xmp (LR/C1 compatible XMP from suggested_iptc).
    If sidecar_dir is given, writes there instead of next to the image.
    Does not modify the original file.
    Returns dict of written paths.
    """
    p = Path(image_path)
    base = p.stem
    if sidecar_dir:
        out_dir = Path(sidecar_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        argus_sidecar = out_dir / f"{base}.argus.json"
    else:
        argus_sidecar = p.with_suffix(".argus.json")

    argus_sidecar.write_text(json.dumps(analysis_data, indent=2, ensure_ascii=False))
    written = {"argus": argus_sidecar}

    iptc = analysis_data.get("suggested_iptc") or {}
    if iptc:
        if sidecar_dir:
            iptc_sidecar = out_dir / f"{base}.iptc.json"
            xmp_sidecar = out_dir / f"{base}.xmp"
        else:
            iptc_sidecar = p.with_suffix(".iptc.json")
            xmp_sidecar = p.with_suffix(".xmp")
        iptc_sidecar.write_text(json.dumps(iptc, indent=2, ensure_ascii=False))
        written["iptc"] = iptc_sidecar
        xmp = _generate_xmp(analysis_data)
        if xmp:
            xmp_sidecar.write_text(xmp, encoding="utf-8")
            written["xmp"] = xmp_sidecar

    return written


def _generate_xmp(analysis_data: dict) -> str:
    """Generate minimal LR-compatible XMP sidecar from argus data + suggested_iptc.
    Uses dc and Iptc4xmpCore namespaces for headline, description, keywords.
    Safe for mock data.
    """
    iptc = analysis_data.get("suggested_iptc") or {}
    headline = iptc.get("headline", "")
    caption = iptc.get("caption", "")
    keywords = iptc.get("keywords", []) or []
    # Also pull from top level if present
    if not keywords:
        keywords = analysis_data.get("keywords", []) or []

    # Escape basic XML
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    kw_xml = "\n".join(f'        <rdf:li>{esc(kw)}</rdf:li>' for kw in keywords)

    xmp = f'''<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d">
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Argus">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
 <rdf:Description rdf:about=""
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/"
   xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">
  <dc:title>
   <rdf:Alt>
    <rdf:li xml:lang="x-default">{esc(headline)}</rdf:li>
   </rdf:Alt>
  </dc:title>
  <dc:description>
   <rdf:Alt>
    <rdf:li xml:lang="x-default">{esc(caption)}</rdf:li>
   </rdf:Alt>
  </dc:description>
  <dc:subject>
   <rdf:Bag>
{kw_xml}
   </rdf:Bag>
  </dc:subject>
  <Iptc4xmpCore:Headline>{esc(headline)}</Iptc4xmpCore:Headline>
  <Iptc4xmpCore:Caption>{esc(caption)}</Iptc4xmpCore:Caption>
 </rdf:Description>
</rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>'''
    return xmp


write_sidecar_func = write_sidecar  # avoid name shadow with form param write_sidecar: bool


def _simulate_cloud_cost(analysis_count: int) -> float:
    """Phase 2/4 stub for cloud inference cost accounting (mock only)."""
    cost = analysis_count * config.CLOUD_COST_PER_IMAGE
    log.info(f"[CLOUD STUB] Simulated cost for {analysis_count} images: ${cost:.4f}")
    return cost


# --- Phase 2 simple background queue worker (mock-safe, thread-based) ---
_queue_lock = threading.Lock()
_worker_running = False
_job_semaphore = threading.Semaphore(config.MAX_CONCURRENT_JOBS) if config.QUEUE_ENABLED else None
_cleanup_counter = 0

def _process_job(job_id: str):
    """Wrapper that releases the semaphore after processing."""
    try:
        _process_job_impl(job_id)
    finally:
        if _job_semaphore:
            _job_semaphore.release()

def _process_job_impl(job_id: str):
    job = db.get_job(job_id)
    if not job or job["status"] != "queued":
        return
    db.update_job(job_id, status="running")
    try:
        p = Path(job["folder"]).expanduser().resolve()
        if not p.is_dir():
            db.update_job(job_id, status="failed", error=f"folder not found: {job['folder']}")
            return

        analyses = vision.analyze_folder(p, limit=job.get("limit_") or 20)

        project_id = job.get("project_id")
        run_id = db.create_run(source=str(p), model=config.VISION_MODEL, project_id=project_id)
        sidecars_written = []
        for a in analyses:
            data = a.model_dump() if hasattr(a, "model_dump") else a
            db.save_photo_analysis(run_id, data)
            if job.get("write_sidecars"):
                scs = write_sidecar(data["image_path"], data, sidecar_dir=job.get("sidecar_dir"))
                sidecars_written.extend(str(v) for v in scs.values())

        con = db.connect()
        try:
            con.execute("UPDATE analysis_runs SET photo_count = ? WHERE id = ?", (len(analyses), run_id))
            con.commit()
        finally:
            db.close(con)

        result = {
            "run_id": run_id,
            "count": len(analyses),
            "sidecars_written": sidecars_written if job.get("write_sidecars") else None,
        }
        if project_id:
            result["project_id"] = project_id
        cost = 0.0
        if config.CLOUD_BACKEND in ("stub", "simulated") or config.CLOUD_BACKEND != "disabled":
            cost = _simulate_cloud_cost(len(analyses))
            result["simulated_cost"] = cost
        db.update_job(job_id, status="done", run_id=run_id, result=result)
        log.info(f"Job {job_id} completed -> run {run_id} (cost={cost}, project={project_id})")
    except Exception as e:
        db.update_job(job_id, status="failed", error=str(e))
        log.exception(f"Job {job_id} failed")


def _queue_worker():
    global _worker_running, _cleanup_counter
    _worker_running = True
    while _worker_running:
        # Periodic cleanup of old jobs
        _cleanup_counter += 1
        if _cleanup_counter % 100 == 0:  # every ~100 seconds
            db.cleanup_old_jobs(days=1)
            log.info("Cleaned old jobs")

        # Find a queued job, respecting concurrency
        jobs = db.list_jobs(10)
        for j in jobs:
            if j["status"] == "queued":
                acquired = True
                if _job_semaphore:
                    acquired = _job_semaphore.acquire(blocking=False)
                if acquired:
                    with _queue_lock:
                        fresh = db.get_job(j["id"])
                        if fresh and fresh["status"] == "queued":
                            db.update_job(j["id"], status="running")
                            threading.Thread(target=_process_job, args=(j["id"],), daemon=True).start()
                            break
                        else:
                            if _job_semaphore:
                                _job_semaphore.release()
        time.sleep(1)


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("argus")

if config.QUEUE_ENABLED:
    threading.Thread(target=_queue_worker, daemon=True).start()
    log.info("Queue worker started (Phase 2)")

app = FastAPI(title="argus / photometa", version="phase2")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
def healthz():
    """Health check. Includes Tailscale info for fleet exposure (Phase 2).
    Call from remote node: curl http://mickey:8010/healthz
    """
    return {
        "status": "ok",
        "service_mode": config.SERVICE_MODE,
        "backend": config.VISION_BACKEND,
        "queue_enabled": config.QUEUE_ENABLED,
        "cloud_backend": config.CLOUD_BACKEND,
        "cloud_cost_per_image": config.CLOUD_COST_PER_IMAGE,
        "tailscale_hint": config.TAILSCALE_HINT,
        "model": config.VISION_MODEL,
        "ollama": config.OLLAMA_HOST,
    }


@app.get("/thumb/{photo_id}")
def get_thumb(photo_id: int):
    """Serve a thumbnail for a stored photo analysis (local-only, trusted paths)."""
    con = db.connect()
    try:
        row = con.execute(
            "SELECT image_path FROM photo_analyses WHERE id = ?",
            (photo_id,),
        ).fetchone()
    finally:
        db.close(con)

    if not row:
        return JSONResponse({"error": "photo not found"}, status_code=404)

    img_path = row["image_path"]
    p = Path(img_path).expanduser().resolve()
    if not p.exists():
        return JSONResponse({"error": "source image not found on disk"}, status_code=404)

    try:
        thumb_bytes = make_thumbnail(p)
        return StreamingResponse(io.BytesIO(thumb_bytes), media_type="image/jpeg")
    except Exception as e:
        log.exception("failed to generate thumb for photo %s", photo_id)
        return JSONResponse({"error": f"thumb error: {e}"}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    recent = [dict(r) for r in db.list_recent_runs(limit=6)]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "recent_runs": recent,
            "model": config.VISION_MODEL,
        },
    )


@app.post("/analyze", response_class=JSONResponse)
async def analyze_single(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    write_sidecar: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
):
    """Analyze one image. Prefer local `path` for real work (no upload).
    If write_sidecar=True, also writes sidecars (to sidecar_dir if given).
    Phase 3: client_id loads & applies learned preferences (mock-safe).
    """
    if not file and not path:
        return JSONResponse({"error": "provide file or local path"}, status_code=400)

    tmp_path = None
    try:
        if file:
            tmp_path = config.DATA_DIR / f"tmp_{file.filename}"
            tmp_path.write_bytes(await file.read())
            image_path = tmp_path
        else:
            image_path = Path(path).expanduser().resolve()
            if not image_path.is_file():
                return JSONResponse({"error": f"file not found: {path}"}, status_code=404)

        prefs = db.get_preferences(client_id) if client_id else None
        if client_id:
            stats = db.get_client_history_stats(client_id)
            bias = stats.get("bias", 0.0)
            prefs = dict(prefs or {})
            prefs["culling_bias"] = prefs.get("culling_bias", 0.0) + bias
        result = vision.analyze_image(str(image_path), model=model, prefs=prefs)
        data = result.model_dump() if hasattr(result, "model_dump") else result
        if client_id:
            data["client_id"] = client_id  # provenance

        # Persist single uploads too (consistent with folder + docs/README)
        run_id = db.create_run(source=str(image_path), model=model or config.VISION_MODEL)
        db.save_photo_analysis(run_id, data)

        # Set count for the single
        con = db.connect()
        try:
            con.execute("UPDATE analysis_runs SET photo_count = 1 WHERE id = ?", (run_id,))
            con.commit()
        finally:
            db.close(con)

        out = dict(data)
        out["run_id"] = run_id
        out["run_url"] = f"/runs/{run_id}"

        if write_sidecar:
            if tmp_path is None:  # only for local path, not uploaded tmp
                scs = write_sidecar_func(str(image_path), data, sidecar_dir=sidecar_dir)
                out["sidecars"] = {k: str(v) for k, v in scs.items()}
            else:
                out["sidecar_warning"] = "sidecar not written for uploaded file (tmp deleted)"

        return out
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.post("/analyze-folder", response_class=JSONResponse)
def analyze_folder_endpoint(
    folder: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    limit: int = Form(20),
    write_sidecars: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    mise_gallery_id: Optional[int] = Form(None),
    mise_project_id: Optional[int] = Form(None),
    client_id: Optional[str] = Form(None),
):
    """Analyze a local folder. This is the main dogfood path for Phase 0.
    - If QUEUE_ENABLED, returns job_id immediately and processes in background.
    - write_sidecars / sidecar_dir supported for both sync and queued paths.
    Phase 3: supports direct mise gallery import.
      - Pass explicit folder (can point at a mise .../media/<id>/original or the gallery root).
      - Or pass mise_gallery_id (+ ARGUS_MISE_MEDIA_ROOT on server) to auto-resolve using
        mise layout pattern (MEDIA/<gallery_id>/original). mise_project_id for labeling.
    Phase 3 slice 4: client_id loads learned preferences and applies during (mock) analysis.
    """
    mise_info = {}
    if mise_gallery_id is not None:
        mise_info["gallery_id"] = mise_gallery_id
    if mise_project_id is not None:
        mise_info["project_id"] = mise_project_id

    effective_folder = folder
    if not effective_folder:
        if mise_gallery_id is not None and config.MISE_MEDIA_ROOT:
            effective_folder = str(config.MISE_MEDIA_ROOT / str(mise_gallery_id) / "original")
        else:
            return JSONResponse({"error": "folder (or mise_gallery_id with ARGUS_MISE_MEDIA_ROOT) required"}, status_code=400)

    p = Path(effective_folder).expanduser().resolve()
    # Support passing the gallery base dir instead of /original subdir (mise pattern)
    if not p.is_dir() and mise_gallery_id is not None:
        alt = p / "original" if p.name != "original" else p
        if alt.is_dir():
            p = alt
            effective_folder = str(p)

    if not p.is_dir():
        return JSONResponse({"error": f"folder not found or not a dir: {effective_folder}"}, status_code=400)

    source = str(p)
    if mise_info:
        mid = ",".join(f"{k}={v}" for k, v in mise_info.items())
        source = f"mise:{mid}|{source}"

    project_id = str(mise_project_id) if mise_project_id is not None else None
    prefs = db.get_preferences(client_id) if client_id else None
    if client_id:
        stats = db.get_client_history_stats(client_id)
        bias = stats.get("bias", 0.0)
        prefs = dict(prefs or {})
        prefs["culling_bias"] = prefs.get("culling_bias", 0.0) + bias

    if config.QUEUE_ENABLED:
        job_id = db.create_job(str(p), limit or 20, write_sidecars, sidecar_dir, project_id=project_id)
        resp = {"job_id": job_id, "status": "queued", "source": source}
        if mise_info:
            resp["mise"] = mise_info
        if project_id:
            resp["project_id"] = project_id
        if client_id:
            resp["client_id"] = client_id
        return resp

    # sync path (original behavior)
    model = model or config.VISION_MODEL
    run_id = db.create_run(source=source, model=model, project_id=project_id)

    analyses = vision.analyze_folder(p, model=model, limit=limit, client_id=client_id, prefs=prefs)

    sidecars_written = []
    for a in analyses:
        data = a.model_dump() if hasattr(a, "model_dump") else a
        db.save_photo_analysis(run_id, data)
        if write_sidecars:
            scs = write_sidecar(data["image_path"], data, sidecar_dir=sidecar_dir)
            sidecars_written.extend(str(p) for p in scs.values())

    con = db.connect()
    try:
        con.execute("UPDATE analysis_runs SET photo_count = ? WHERE id = ?", (len(analyses), run_id))
        con.commit()
    finally:
        db.close(con)

    photos = [a.model_dump() if hasattr(a, "model_dump") else a for a in analyses]

    resp = {
        "run_id": run_id,
        "source": source,
        "model": model,
        "count": len(analyses),
        "photos": photos,
    }
    if mise_info:
        resp["mise"] = mise_info
    if write_sidecars:
        resp["sidecars_written"] = sidecars_written
        if sidecar_dir:
            resp["sidecar_dir"] = sidecar_dir
    return resp


@app.post("/import/mise-project", response_class=JSONResponse)
def import_mise_project(
    mise_project_id: int = Form(...),
    gallery_path: Optional[str] = Form(None),
    mise_gallery_id: Optional[int] = Form(None),
    limit: int = Form(50),
    write_sidecars: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
):
    """Phase 3 slice 3: Batch 'analyze entire project'.
    Wires the Phase 2 queue, sidecar writing, and simulated costs under a project concept.
    Ties to mise via project_id (and optional gallery for resolution using mise media layout).
    Returns job (queued) or run with project_id.
    Phase 3 slice 4: also supports client_id for prefs.
    """
    # Reuse the resolution from analyze logic
    effective_folder = gallery_path
    if not effective_folder and mise_gallery_id is not None and config.MISE_MEDIA_ROOT:
        effective_folder = str(config.MISE_MEDIA_ROOT / str(mise_gallery_id) / "original")
    if not effective_folder:
        # fallback to project-based path if convention used
        if config.MISE_MEDIA_ROOT:
            effective_folder = str(config.MISE_MEDIA_ROOT / f"project-{mise_project_id}" / "original")
        else:
            return JSONResponse({"error": "gallery_path or (mise_gallery_id + ARGUS_MISE_MEDIA_ROOT) required for project"}, status_code=400)

    p = Path(effective_folder).expanduser().resolve()
    if not p.is_dir() and mise_gallery_id is not None:
        alt = p / "original" if p.name != "original" else p
        if alt.is_dir():
            p = alt
    if not p.is_dir():
        return JSONResponse({"error": f"could not resolve project photos dir: {effective_folder}"}, status_code=400)

    source = f"mise:project={mise_project_id}|{p}"
    pid = str(mise_project_id)
    prefs = db.get_preferences(client_id) if client_id else None

    if config.QUEUE_ENABLED:
        job_id = db.create_job(str(p), limit or 50, write_sidecars, sidecar_dir, project_id=pid)
        return {
            "job_id": job_id,
            "status": "queued",
            "source": source,
            "project_id": pid,
            "mise_project_id": mise_project_id,
            "mise_gallery_id": mise_gallery_id,
            "client_id": client_id,
        }

    # sync fallback (rare)
    run_id = db.create_run(source=source, model=model or config.VISION_MODEL, project_id=pid)
    analyses = vision.analyze_folder(p, model=model, limit=limit)
    # ... (abbreviated for batch focus; sidecars/costs wired via job path primarily)
    sidecars_written = []
    for a in analyses:
        data = a.model_dump() if hasattr(a, "model_dump") else a
        db.save_photo_analysis(run_id, data)
        if write_sidecars:
            scs = write_sidecar(data["image_path"], data, sidecar_dir=sidecar_dir)
            sidecars_written.extend(str(v) for v in scs.values())
    con = db.connect()
    try:
        con.execute("UPDATE analysis_runs SET photo_count = ? WHERE id = ?", (len(analyses), run_id))
        con.commit()
    finally:
        db.close(con)
    resp = {"run_id": run_id, "source": source, "project_id": pid, "count": len(analyses)}
    if write_sidecars:
        resp["sidecars_written"] = sidecars_written
    cost = _simulate_cloud_cost(len(analyses)) if config.CLOUD_BACKEND in ("stub", "simulated") or config.CLOUD_BACKEND != "disabled" else 0.0
    resp["simulated_cost"] = cost
    return resp


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def view_run(run_id: int, request: Request):
    data = db.get_full_run(run_id)
    if not data:
        return HTMLResponse("Run not found", status_code=404)

    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": data["run"],
            "photos": data["photos"],
            "model": data["run"].get("model"),
        },
    )


@app.get("/runs", response_class=JSONResponse)
def list_runs():
    return {"runs": [dict(r) for r in db.list_recent_runs()]}


@app.get("/runs/{run_id}/export", response_class=JSONResponse)
def export_run(run_id: int):
    """Return complete structured run + photos for download / downstream use."""
    data = db.get_full_run(run_id)
    if not data:
        return JSONResponse({"error": "run not found"}, status_code=404)
    return data


@app.get("/runs/{run_id}/export.csv")
def export_run_csv(run_id: int):
    """Phase 3/4: CSV export of photos for batch consumption by mise/mnemosyne."""
    data = db.get_full_run(run_id)
    if not data:
        return JSONResponse({"error": "run not found"}, status_code=404)

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id", "basename", "image_path", "shot_type",
            "keeper_score", "hero_potential", "technical_quality",
            "keywords", "alt_text"
        ]
    )
    writer.writeheader()
    for p in data.get("photos", []):
        c = p.get("culling", {}) or {}
        row = {
            "id": p.get("id"),
            "basename": p.get("basename"),
            "image_path": p.get("image_path"),
            "shot_type": p.get("shot_type"),
            "keeper_score": c.get("keeper_score"),
            "hero_potential": c.get("hero_potential"),
            "technical_quality": c.get("technical_quality"),
            "keywords": ",".join(p.get("keywords", [])),
            "alt_text": p.get("alt_text"),
        }
        writer.writerow(row)
    csv_content = output.getvalue()
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}.csv"'}
    )


@app.get("/runs/{run_id}/photo/{photo_id}/sidecar", response_class=JSONResponse)
def photo_sidecar(run_id: int, photo_id: int):
    """Return a single photo's structured data (sidecar JSON)."""
    data = db.get_full_run(run_id)
    if not data:
        return JSONResponse({"error": "run not found"}, status_code=404)
    for p in data.get("photos", []):
        if p.get("id") == photo_id:
            return p
    return JSONResponse({"error": "photo not found"}, status_code=404)


@app.post("/runs/{run_id}/write-sidecars", response_class=JSONResponse)
def write_sidecars_for_run(run_id: int, sidecar_dir: Optional[str] = Form(None)):
    """Write sidecars for an existing run (Phase 1). Useful for re-exporting."""
    data = db.get_full_run(run_id)
    if not data:
        return JSONResponse({"error": "run not found"}, status_code=404)
    written = []
    for p in data.get("photos", []):
        scs = write_sidecar(p["image_path"], p, sidecar_dir=sidecar_dir)
        written.extend(str(v) for v in scs.values())
    return {"run_id": run_id, "sidecars_written": written, "sidecar_dir": sidecar_dir}


@app.get("/jobs/costs", response_class=JSONResponse)
def get_costs(summary: bool = False):
    """Phase 2: expose simulated costs for accounting.
    If summary=true, just total.
    """
    jobs = db.list_jobs(100)
    costs = []
    total = 0.0
    by_project = {}
    for row in jobs:
        j = dict(row)
        if j["status"] == "done" and j.get("result"):
            res = j["result"] if isinstance(j["result"], dict) else json.loads(j["result"])
            c = float(res.get("simulated_cost", 0))
            pid = j.get("project_id") or res.get("project_id")
            entry = {"job_id": j["id"], "cost": c, "project_id": pid}
            costs.append(entry)
            total += c
            if pid:
                by_project.setdefault(pid, 0.0)
                by_project[pid] += c
    if summary:
        out = {"total_cost": round(total, 4), "num_jobs": len(costs)}
        if by_project:
            out["by_project"] = {k: round(v, 4) for k, v in by_project.items()}
        return out
    return {"costs": costs, "total_cost": round(total, 4), "by_project": {k: round(v, 4) for k, v in by_project.items()} if by_project else None}


@app.get("/jobs", response_class=JSONResponse)
def list_jobs_endpoint(limit: int = 20):
    return {"jobs": [dict(j) for j in db.list_jobs(limit)]}


# --- Phase 3 slice 4: learned preferences API (minimal) ---
@app.get("/preferences", response_class=JSONResponse)
def get_prefs(client_id: Optional[str] = None, style: Optional[str] = None):
    prefs = db.get_preferences(client_id, style)
    return {"client_id": client_id, "style": style, "prefs": prefs}


@app.post("/preferences", response_class=JSONResponse)
def set_prefs(client_id: str = Form(...), prefs: str = Form(...), style: Optional[str] = Form(None)):
    """prefs is JSON string. Example: {"keyword_boosts": ["warm tones"], "culling_bias": 0.15, "shot_type_preference": ["hero_plate"]}"""
    try:
        prefs_dict = json.loads(prefs)
    except Exception as e:
        return JSONResponse({"error": f"invalid prefs json: {e}"}, status_code=400)
    pid = db.set_preferences(client_id, prefs_dict, style)
    return {"ok": True, "id": pid, "client_id": client_id, "style": style, "prefs": prefs_dict}


@app.get("/jobs/{job_id}", response_class=JSONResponse)
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job


def cli_main():
    """Entry point for the `argus` CLI (wired in pyproject.toml).

    Supports direct analysis (bypassing the HTTP server) with mock backend.
    Use ARGUS_VISION_BACKEND=mock (default in service).
    """
    import argparse
    import json as jsonlib

    parser = argparse.ArgumentParser(description="argus photometa CLI (supports Phase 3+ features: mise import, client prefs, XMP sidecars)")
    parser.add_argument("folders", nargs="*", help="folder(s) to analyze (optional if using --mise-gallery-id + ARGUS_MISE_MEDIA_ROOT)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--write-sidecars", action="store_true", help="write .argus.json and .iptc.json (and .xmp) sidecars")
    parser.add_argument("--sidecar-dir", default=None, help="collect sidecars in this dir instead of next to images")
    parser.add_argument("--config", default=None, help="json config file for defaults (e.g. limit, sidecar_dir)")
    parser.add_argument("--port", type=int, default=config.PORT, help="(unused for direct CLI; remnant)")
    # Phase 3 features
    parser.add_argument("--mise-gallery-id", type=int, default=None, help="mise gallery id for import (resolves via ARGUS_MISE_MEDIA_ROOT if no folders)")
    parser.add_argument("--mise-project-id", type=int, default=None, help="optional mise project id for source labeling")
    parser.add_argument("--client-id", default=None, help="client id for learned preferences")
    args = parser.parse_args()

    # load config if provided
    cfg = {}
    if args.config:
        with open(args.config) as f:
            cfg = jsonlib.load(f)
    limit = args.limit or cfg.get("limit", 5)
    sidecar_dir = args.sidecar_dir or cfg.get("sidecar_dir")
    client_id = args.client_id

    from . import vision, db, config as argus_config
    total = 0

    folders = list(args.folders) if args.folders else []
    mise_info_cli = {}
    if args.mise_gallery_id is not None:
        mise_info_cli["gallery_id"] = args.mise_gallery_id
    if args.mise_project_id is not None:
        mise_info_cli["project_id"] = args.mise_project_id

    if not folders and mise_info_cli.get("gallery_id") is not None and argus_config.MISE_MEDIA_ROOT:
        # auto-resolve for direct mise import via CLI
        gid = mise_info_cli["gallery_id"]
        auto = argus_config.MISE_MEDIA_ROOT / str(gid) / "original"
        folders = [str(auto)]
        print(f"Resolved mise gallery {gid} -> {auto}")

    if not folders:
        print("No folders (provide folders or --mise-gallery-id with ARGUS_MISE_MEDIA_ROOT)")
        return

    prefs = db.get_preferences(client_id) if client_id else None
    if client_id:
        print(f"Applying preferences for client_id={client_id}")

    for folder in folders:
        print(f"Analyzing {folder} (limit={limit}) ...")
        res = vision.analyze_folder(folder, limit=limit, prefs=prefs)
        source = folder
        if client_id:
            source = f"client:{client_id}|{source}"
        if mise_info_cli:
            mid = ",".join(f"{k}={v}" for k, v in mise_info_cli.items())
            source = f"mise:{mid}|{source}"
        proj = str(args.mise_project_id) if args.mise_project_id else None
        run_id = db.create_run(source=source, model=argus_config.VISION_MODEL, project_id=proj)
        for i, a in enumerate(res):
            data = a.model_dump() if hasattr(a, "model_dump") else a
            db.save_photo_analysis(run_id, data)
            if args.write_sidecars:
                write_sidecar(data["image_path"], data, sidecar_dir=sidecar_dir)
            if i % 5 == 0:
                print(f"  progress: {i+1}/{len(res)}")
        con = db.connect()
        con.execute("UPDATE analysis_runs SET photo_count=? WHERE id=?", (len(res), run_id))
        con.commit()
        con.close()
        print(f"Run {run_id} created with {len(res)} photos (source={source})")
        total += len(res)
        if args.write_sidecars:
            msg = "Sidecars written"
            if sidecar_dir:
                msg += f" to {sidecar_dir}"
            else:
                msg += " next to images"
            print(msg)
    print(f"Done. Total photos: {total}")


if __name__ == "__main__":
    cli_main()
