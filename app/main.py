"""Argus / photometa — minimal FastAPI for Phase 0 vision & metadata analysis.

Local-first. Drop a folder of real photos, get structured photography-grade output.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from . import config, db, vision
from .vision import AnalysisResult, Culling  # for typed responses (C)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("argus")

app = FastAPI(title="argus / photometa", version="phase0")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Jinja helpers
templates.env.filters["basename"] = lambda p: os.path.basename(str(p)) if p else ""
templates.env.filters["tojson"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False) if v else "{}"


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "model": config.VISION_MODEL,
        "ollama": config.OLLAMA_HOST,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    recent = db.list_recent_runs(limit=6)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "recent_runs": recent,
            "model": config.VISION_MODEL,
        },
    )


@app.post("/analyze", response_class=JSONResponse)
async def analyze_single(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
):
    """Analyze one image. Prefer local `path` for real work (no upload)."""
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

        result = vision.analyze_image(str(image_path), model=model)
        return result.model_dump() if hasattr(result, "model_dump") else result
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.post("/analyze-folder", response_class=JSONResponse)
def analyze_folder_endpoint(
    folder: str = Form(...),
    model: Optional[str] = Form(None),
    limit: int = Form(20),
):
    """Analyze a local folder. This is the main dogfood path for Phase 0."""
    p = Path(folder).expanduser().resolve()
    if not p.is_dir():
        return JSONResponse({"error": f"folder not found or not a dir: {folder}"}, status_code=400)

    model = model or config.VISION_MODEL
    run_id = db.create_run(source=str(p), model=model)

    analyses = vision.analyze_folder(p, model=model, limit=limit)

    for a in analyses:
        # Support both Pydantic models and dicts
        data = a.model_dump() if hasattr(a, "model_dump") else a
        db.save_photo_analysis(run_id, data)

    con = db.connect()
    try:
        con.execute("UPDATE analysis_runs SET photo_count = ? WHERE id = ?", (len(analyses), run_id))
        con.commit()
    finally:
        db.close(con)

    # Return typed data as dicts for JSON
    photos = [a.model_dump() if hasattr(a, "model_dump") else a for a in analyses]

    return {
        "run_id": run_id,
        "source": str(p),
        "model": model,
        "count": len(analyses),
        "photos": photos,
    }


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def view_run(run_id: int, request: Request):
    run = db.get_run(run_id)
    if not run:
        return HTMLResponse("Run not found", status_code=404)

    raw_photos = db.get_photos_for_run(run_id)
    photos = []
    for row in raw_photos:
        p = dict(row)
        for key in ("keywords", "culling", "suggested_iptc"):
            val = p.get(key)
            if val:
                try:
                    p[key] = json.loads(val)
                except Exception:
                    p[key] = {} if key != "keywords" else []
            else:
                p[key] = {} if key != "keywords" else []
        if not p.get("shot_type"):
            p["shot_type"] = row.get("shot_type") or "other"
        photos.append(p)

    return templates.TemplateResponse(
        "run.html",
        {
            "request": request,
            "run": dict(run),
            "photos": photos,
            "model": run["model"],
        },
    )


@app.get("/runs", response_class=JSONResponse)
def list_runs():
    return {"runs": [dict(r) for r in db.list_recent_runs()]}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
