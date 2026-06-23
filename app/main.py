"""Argus / photometa FastAPI application."""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, metrics, service
from .auth import require_bearer
from .jobs import JobWorker
from .sidecars import write_sidecar
from .vision import make_thumbnail

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("argus")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    worker = JobWorker()
    worker.start()
    app.state.job_worker = worker
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="argus / photometa", version="phase4", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "service_mode": config.SERVICE_MODE,
        "backend": config.VISION_BACKEND,
        "queue_enabled": config.QUEUE_ENABLED,
        "cloud_backend": config.CLOUD_BACKEND,
        "cloud_cost_per_image": config.CLOUD_COST_PER_IMAGE,
        "tailscale_hint": config.TAILSCALE_HINT,
        "auth_enabled": bool(config.API_TOKEN),
        "model": config.VISION_MODEL,
        "ollama": config.OLLAMA_HOST,
    }


@app.get("/metrics", response_class=JSONResponse)
def get_metrics():
    return metrics.snapshot()


@app.get("/clients/{client_id}/history", response_class=JSONResponse)
def client_history(client_id: str):
    return db.get_client_history_stats(client_id)


@app.get("/thumb/{photo_id}")
def get_thumb(photo_id: int):
    image_path = db.get_photo_image_path(photo_id)
    if not image_path:
        return error("photo not found", 404)

    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        return error("source image not found on disk", 404)

    try:
        thumb_bytes = make_thumbnail(path)
        return StreamingResponse(io.BytesIO(thumb_bytes), media_type="image/jpeg")
    except Exception as exc:
        log.exception("failed to generate thumb for photo %s", photo_id)
        return error(f"thumb error: {exc}", 500)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    recent = [dict(row) for row in db.list_recent_runs(limit=6)]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"recent_runs": recent, "model": config.VISION_MODEL},
    )


@app.post("/analyze", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
async def analyze_single(
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    write_sidecar: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
):
    if not file and not path:
        return error("provide file or local path", 400)

    tmp_path: Path | None = None
    try:
        if file is not None:
            safe_name = Path(file.filename or "upload.jpg").name
            tmp_path = config.DATA_DIR / f"upload_{uuid.uuid4().hex}_{safe_name}"
            tmp_path.write_bytes(await file.read())
            image_path = tmp_path
        else:
            image_path = Path(path or "").expanduser().resolve()
            if not image_path.is_file():
                return error(f"file not found: {path}", 404)

        out = service.analyze_single_image(
            image_path=image_path,
            model=model,
            client_id=client_id,
        )
        metrics.inc("analyze_single")
        metrics.inc("photos_analyzed")
        if write_sidecar:
            if tmp_path is None:
                written = write_sidecar(str(image_path), out, sidecar_dir=sidecar_dir)
                out["sidecars"] = {key: str(value) for key, value in written.items()}
            else:
                out["sidecar_warning"] = "sidecar not written for uploaded file (tmp deleted)"
        return out
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.post("/analyze-folder", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
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
    path, mise_info, attempted = service.resolve_mise_folder(
        folder=folder,
        mise_gallery_id=mise_gallery_id,
        mise_project_id=mise_project_id,
    )
    if path is None:
        return error("folder (or mise_gallery_id with ARGUS_MISE_MEDIA_ROOT) required", 400)
    if not path.is_dir():
        return error(f"folder not found or not a dir: {attempted}", 400)

    project_id = str(mise_project_id) if mise_project_id is not None else None
    source = service.source_label(path, mise_info=mise_info, client_id=client_id)
    model_name = model or config.VISION_MODEL

    if config.QUEUE_ENABLED:
        job_id = db.create_job(
            str(path),
            limit or 20,
            write_sidecars,
            sidecar_dir,
            project_id=project_id,
            source=source,
            model=model_name,
            client_id=client_id,
        )
        response = {"job_id": job_id, "status": "queued", "source": source}
        if mise_info:
            response["mise"] = mise_info
        if project_id:
            response["project_id"] = project_id
        if client_id:
            response["client_id"] = client_id
        return response

    result = service.analyze_folder_run(
        folder=path,
        source=source,
        model=model_name,
        limit=limit,
        project_id=project_id,
        write_sidecars=write_sidecars,
        sidecar_dir=sidecar_dir,
        client_id=client_id,
    )
    metrics.inc("analyze_folder")
    metrics.inc("photos_analyzed", result["count"])
    if mise_info:
        result["mise"] = mise_info
    if not write_sidecars:
        result.pop("sidecars_written", None)
    elif sidecar_dir:
        result["sidecar_dir"] = sidecar_dir
    return result


@app.post("/import/mise-project", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
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
    path, _, attempted = service.resolve_mise_folder(
        folder=gallery_path,
        mise_gallery_id=mise_gallery_id,
        mise_project_id=mise_project_id,
    )
    if path is None:
        return error("gallery_path or (mise_gallery_id + ARGUS_MISE_MEDIA_ROOT) required for project", 400)
    if not path.is_dir():
        return error(f"could not resolve project photos dir: {attempted}", 400)

    project_id = str(mise_project_id)
    mise_info = {"project": mise_project_id}
    if mise_gallery_id is not None:
        mise_info["gallery_id"] = mise_gallery_id
    source = service.source_label(path, mise_info=mise_info, client_id=client_id)
    model_name = model or config.VISION_MODEL

    if config.QUEUE_ENABLED:
        job_id = db.create_job(
            str(path),
            limit or 50,
            write_sidecars,
            sidecar_dir,
            project_id=project_id,
            source=source,
            model=model_name,
            client_id=client_id,
        )
        return {
            "job_id": job_id,
            "status": "queued",
            "source": source,
            "project_id": project_id,
            "mise_project_id": mise_project_id,
            "mise_gallery_id": mise_gallery_id,
            "client_id": client_id,
        }

    result = service.analyze_folder_run(
        folder=path,
        source=source,
        model=model_name,
        limit=limit,
        project_id=project_id,
        write_sidecars=write_sidecars,
        sidecar_dir=sidecar_dir,
        client_id=client_id,
    )
    metrics.inc("analyze_folder")
    metrics.inc("photos_analyzed", result["count"])
    result["mise_project_id"] = mise_project_id
    result["mise_gallery_id"] = mise_gallery_id
    if service.simulated_cloud_cost and config.CLOUD_BACKEND != "disabled":
        result["simulated_cost"] = service.simulated_cloud_cost(result["count"])
    if not write_sidecars:
        result.pop("sidecars_written", None)
    return result


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def view_run(run_id: int, request: Request):
    data = db.get_full_run(run_id)
    if not data:
        return HTMLResponse("Run not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "run.html",
        {"run": data["run"], "photos": data["photos"], "model": data["run"].get("model")},
    )


@app.get("/runs", response_class=JSONResponse)
def list_runs():
    return {"runs": [dict(row) for row in db.list_recent_runs()]}


@app.get("/runs/{run_id}/export", response_class=JSONResponse)
def export_run(run_id: int):
    data = db.get_full_run(run_id)
    if not data:
        return error("run not found", 404)
    return data


@app.get("/runs/{run_id}/export.csv")
def export_run_csv(run_id: int):
    data = db.get_full_run(run_id)
    if not data:
        return error("run not found", 404)

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "basename",
            "image_path",
            "shot_type",
            "keeper_score",
            "hero_potential",
            "technical_quality",
            "keywords",
            "alt_text",
        ],
    )
    writer.writeheader()
    for photo in data.get("photos", []):
        culling = photo.get("culling", {}) or {}
        writer.writerow(
            {
                "id": photo.get("id"),
                "basename": photo.get("basename"),
                "image_path": photo.get("image_path"),
                "shot_type": photo.get("shot_type"),
                "keeper_score": culling.get("keeper_score"),
                "hero_potential": culling.get("hero_potential"),
                "technical_quality": culling.get("technical_quality"),
                "keywords": ",".join(photo.get("keywords", [])),
                "alt_text": photo.get("alt_text"),
            }
        )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}.csv"'},
    )


@app.get("/runs/{run_id}/photo/{photo_id}/sidecar", response_class=JSONResponse)
def photo_sidecar(run_id: int, photo_id: int):
    data = db.get_full_run(run_id)
    if not data:
        return error("run not found", 404)
    for photo in data.get("photos", []):
        if photo.get("id") == photo_id:
            return photo
    return error("photo not found", 404)


@app.post(
    "/runs/{run_id}/write-sidecars",
    response_class=JSONResponse,
    dependencies=[Depends(require_bearer)],
)
def write_sidecars_for_run(run_id: int, sidecar_dir: Optional[str] = Form(None)):
    data = db.get_full_run(run_id)
    if not data:
        return error("run not found", 404)
    written = []
    for photo in data.get("photos", []):
        paths = write_sidecar(photo["image_path"], photo, sidecar_dir=sidecar_dir)
        written.extend(str(path) for path in paths.values())
    return {"run_id": run_id, "sidecars_written": written, "sidecar_dir": sidecar_dir}


@app.get("/jobs/costs", response_class=JSONResponse)
def get_costs(summary: bool = False):
    costs = []
    total = 0.0
    by_project: dict[str, float] = {}
    for row in db.list_jobs(100):
        job = dict(row)
        result = json.loads(job["result"]) if job.get("result") else {}
        if job["status"] != "done" or not result:
            continue
        cost = float(result.get("simulated_cost", 0))
        project_id = job.get("project_id") or result.get("project_id")
        costs.append({"job_id": job["id"], "cost": cost, "project_id": project_id})
        total += cost
        if project_id:
            by_project[project_id] = by_project.get(project_id, 0.0) + cost

    by_project = {key: round(value, 4) for key, value in by_project.items()}
    if summary:
        out = {"total_cost": round(total, 4), "num_jobs": len(costs)}
        if by_project:
            out["by_project"] = by_project
        return out
    return {"costs": costs, "total_cost": round(total, 4), "by_project": by_project or None}


@app.get("/jobs", response_class=JSONResponse)
def list_jobs_endpoint(limit: int = 20):
    return {"jobs": [dict(row) for row in db.list_jobs(limit)]}


@app.get("/jobs/{job_id}", response_class=JSONResponse)
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        return error("job not found", 404)
    return job


@app.get("/preferences", response_class=JSONResponse)
def get_prefs(client_id: Optional[str] = None, style: Optional[str] = None):
    return {"client_id": client_id, "style": style, "prefs": db.get_preferences(client_id, style)}


@app.post("/preferences", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
def set_prefs(
    client_id: str = Form(...),
    prefs: str = Form(...),
    style: Optional[str] = Form(None),
):
    try:
        prefs_dict = json.loads(prefs)
    except Exception as exc:
        return error(f"invalid prefs json: {exc}", 400)
    pref_id = db.set_preferences(client_id, prefs_dict, style)
    metrics.inc("preferences_writes")
    return {"ok": True, "id": pref_id, "client_id": client_id, "style": style, "prefs": prefs_dict}


def cli_main():
    import argparse

    parser = argparse.ArgumentParser(description="argus photometa CLI")
    parser.add_argument("folders", nargs="*", help="folder(s) to analyze")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--write-sidecars", action="store_true")
    parser.add_argument("--sidecar-dir", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--mise-gallery-id", type=int, default=None)
    parser.add_argument("--mise-project-id", type=int, default=None)
    parser.add_argument("--client-id", default=None)
    args = parser.parse_args()

    cfg = {}
    if args.config:
        cfg = json.loads(Path(args.config).read_text())

    limit = args.limit or cfg.get("limit", 5)
    sidecar_dir = args.sidecar_dir or cfg.get("sidecar_dir")
    folders = list(args.folders)

    if not folders and args.mise_gallery_id is not None and config.MISE_MEDIA_ROOT:
        auto = config.MISE_MEDIA_ROOT / str(args.mise_gallery_id) / "original"
        folders = [str(auto)]
        print(f"Resolved mise gallery {args.mise_gallery_id} -> {auto}")

    if not folders:
        print("No folders (provide folders or --mise-gallery-id with ARGUS_MISE_MEDIA_ROOT)")
        return

    total = 0
    for folder in folders:
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            print(f"Skipping missing folder: {folder}")
            continue
        mise_info = {}
        if args.mise_gallery_id is not None:
            mise_info["gallery_id"] = args.mise_gallery_id
        if args.mise_project_id is not None:
            mise_info["project_id"] = args.mise_project_id
        source = service.source_label(path, mise_info=mise_info, client_id=args.client_id)
        print(f"Analyzing {path} (limit={limit}) ...")
        result = service.analyze_folder_run(
            folder=path,
            source=source,
            limit=limit,
            project_id=str(args.mise_project_id) if args.mise_project_id else None,
            write_sidecars=args.write_sidecars,
            sidecar_dir=sidecar_dir,
            client_id=args.client_id,
        )
        total += result["count"]
        print(f"Run {result['run_id']} created with {result['count']} photos (source={source})")
        if args.write_sidecars:
            print(f"Sidecars written: {len(result['sidecars_written'])}")
    print(f"Done. Total photos: {total}")


if __name__ == "__main__":
    cli_main()
