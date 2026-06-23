"""Argus / photometa FastAPI application."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import config, db, metrics, service
from .auth import UI_TOKEN_COOKIE, require_bearer, verify_api_access
from .callbacks import is_allowed_callback_url
from .jobs import JobWorker
from .sidecars import write_sidecar
from .vision import make_thumbnail
from .vision_status import vision_status

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("argus")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["basename"] = os.path.basename


def _ui_context(**extra) -> dict:
    """Shared template context for HTML pages (vision banner + model)."""
    ctx = {
        "model": config.VISION_MODEL,
        "vision": vision_status(),
        "auth_required": bool(config.API_TOKEN),
    }
    ctx.update(extra)
    return ctx


def _analyze_folder_response(result: dict) -> JSONResponse:
    return JSONResponse(result)


def _redirect_after_folder_analyze(result: dict) -> RedirectResponse:
    if result.get("mode") == "queued":
        return RedirectResponse(f"/ui/jobs/{result['job_id']}", status_code=303)
    return RedirectResponse(f"/runs/{result['run_id']}", status_code=303)


class PhotoPatch(BaseModel):
    keywords: list[str] | None = None
    keeper_score: float | None = Field(default=None, ge=0.0, le=1.0)
    hero_potential: float | None = Field(default=None, ge=0.0, le=1.0)
    shot_type: str | None = None
    promote_keywords: list[str] | None = None


class JobCreate(BaseModel):
    folder: str
    limit: int = 20
    write_sidecars: bool = False
    sidecar_dir: str | None = None
    client_id: str | None = None
    callback_url: str | None = None
    recursive: bool = False
    model: str | None = None
    project_id: str | None = None
    source: str | None = None


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


app = FastAPI(title="argus / photometa", version="phase9", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _enqueue_folder_job(
    *,
    path: Path,
    source: str,
    model_name: str,
    limit: int,
    write_sidecars: bool,
    sidecar_dir: str | None,
    project_id: str | None,
    client_id: str | None,
    callback_url: str | None,
    recursive: bool,
    extra: dict | None = None,
) -> JSONResponse:
    ok, reason = service.queue_accepting_jobs()
    if not ok:
        return error(reason or "queue saturated", 503)

    if callback_url and not is_allowed_callback_url(callback_url):
        return error("callback_url must be local or tailnet (http/https)", 400)

    job_id = db.create_job(
        str(path),
        limit or 20,
        write_sidecars,
        sidecar_dir,
        project_id=project_id,
        source=source,
        model=model_name,
        client_id=client_id,
        callback_url=callback_url,
        recursive=recursive,
    )
    response = {
        "job_id": job_id,
        "status": "queued",
        "source": source,
        "recursive": recursive,
    }
    if callback_url:
        response["callback_url"] = callback_url
    if extra:
        response.update(extra)
    return JSONResponse(response)


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
        "prometheus_enabled": config.PROMETHEUS_ENABLED,
        "queue_depth": db.queue_depth() if config.QUEUE_ENABLED else 0,
        "model": config.VISION_MODEL,
        "grok_configured": bool(config.XAI_API_KEY),
        "vision_provider": "xai" if config.VISION_BACKEND == "grok" else config.VISION_BACKEND,
    }


@app.get("/vision/status", response_class=JSONResponse)
def get_vision_status():
    return vision_status()


@app.get("/metrics", response_class=JSONResponse)
def get_metrics():
    return metrics.snapshot()


@app.get("/metrics/prometheus", response_class=PlainTextResponse)
def get_metrics_prometheus():
    if not config.PROMETHEUS_ENABLED:
        return PlainTextResponse("prometheus metrics disabled", status_code=404)
    return PlainTextResponse(
        metrics.prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/clients/{client_id}/history", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
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
        _ui_context(recent_runs=recent),
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
    recursive: bool = Form(False),
    callback_url: Optional[str] = Form(None),
):
    try:
        result = service.perform_folder_analyze(
            folder=folder,
            model=model,
            limit=limit,
            write_sidecars=write_sidecars,
            sidecar_dir=sidecar_dir,
            mise_gallery_id=mise_gallery_id,
            mise_project_id=mise_project_id,
            client_id=client_id,
            recursive=recursive,
            callback_url=callback_url,
        )
    except service.AnalyzeError as exc:
        return error(exc.message, exc.status_code)
    return _analyze_folder_response(result)


@app.post("/ui/analyze-folder")
def ui_analyze_folder(
    request: Request,
    folder: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    limit: int = Form(20),
    write_sidecars: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    recursive: Optional[str] = Form(None),
    api_token: Optional[str] = Form(None),
):
    verify_api_access(request, form_token=api_token)
    is_recursive = str(recursive or "").lower() in {"true", "1", "on", "yes"}
    try:
        result = service.perform_folder_analyze(
            folder=folder,
            model=model,
            limit=limit,
            write_sidecars=write_sidecars,
            sidecar_dir=sidecar_dir,
            client_id=client_id,
            recursive=is_recursive,
        )
    except service.AnalyzeError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(
                title="Analyze failed",
                message=exc.message,
                status_code=exc.status_code,
            ),
            status_code=exc.status_code,
        )
    return _redirect_after_folder_analyze(result)


@app.post("/ui/analyze")
async def ui_analyze_single(
    request: Request,
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    api_token: Optional[str] = Form(None),
):
    verify_api_access(request, form_token=api_token)
    if not file and not path:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(title="Analyze failed", message="provide file or local path", status_code=400),
            status_code=400,
        )

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
                return templates.TemplateResponse(
                    request,
                    "error.html",
                    _ui_context(title="Analyze failed", message=f"file not found: {path}", status_code=404),
                    status_code=404,
                )

        out = service.analyze_single_image(
            image_path=image_path,
            model=model,
            client_id=client_id,
        )
        metrics.inc("analyze_single")
        metrics.inc("photos_analyzed")
        return RedirectResponse(f"/runs/{out['run_id']}", status_code=303)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.post("/ui/token")
def ui_set_token(
    request: Request,
    api_token: str = Form(...),
):
    if not config.API_TOKEN:
        return RedirectResponse("/", status_code=303)
    if api_token.strip() != config.API_TOKEN:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(title="Login failed", message="invalid API token", status_code=401),
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        UI_TOKEN_COOKIE,
        api_token.strip(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/ui/logout")
def ui_logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(UI_TOKEN_COOKIE)
    return response


@app.get("/ui/jobs", response_class=HTMLResponse)
def ui_jobs(request: Request, status: Optional[str] = Query(None), limit: int = Query(30, ge=1, le=200)):
    jobs = [dict(row) for row in db.list_jobs(limit, status=status)]
    counts = {
        "queued": db.count_jobs_by_status("queued"),
        "running": db.count_jobs_by_status("running"),
        "done": db.count_jobs_by_status("done"),
        "failed": db.count_jobs_by_status("failed"),
        "dead_letter": db.count_jobs_by_status("dead_letter"),
    }
    return templates.TemplateResponse(
        request,
        "jobs.html",
        _ui_context(jobs=jobs, job_counts=counts, filter_status=status),
    )


@app.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
def ui_job_detail(request: Request, job_id: str):
    job = db.get_job(job_id)
    if not job:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(title="Not found", message=f"job not found: {job_id}", status_code=404),
            status_code=404,
        )
    run_id = job.get("run_id")
    result = {}
    if job.get("result"):
        try:
            result = json.loads(job["result"]) if isinstance(job["result"], str) else job["result"]
        except json.JSONDecodeError:
            result = {}
    if not run_id and isinstance(result, dict):
        run_id = result.get("run_id")
    return templates.TemplateResponse(
        request,
        "job.html",
        _ui_context(job=job, job_result=result, run_id=run_id),
    )


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


def _run_review_context(data: dict, **filters) -> dict:
    photos = data.get("photos") or []
    run = data["run"]
    shot_types = sorted({photo.get("shot_type") or "other" for photo in photos})
    filtered = service.sort_and_filter_photos(photos, **filters)
    return _ui_context(
        run=run,
        photos=filtered,
        all_photos=photos,
        heroes=service.hero_candidates(photos),
        shot_types=shot_types,
        client_id=service.extract_client_id(run.get("source")),
        model=run.get("model") or config.VISION_MODEL,
        sort=filters.get("sort", "keeper"),
        filter_shot_type=filters.get("shot_type"),
        filter_keyword=filters.get("keyword"),
        filter_min_keeper=filters.get("min_keeper"),
    )


@app.get("/runs/compare", response_class=JSONResponse)
def compare_runs(
    a: int = Query(..., description="first run id"),
    b: int = Query(..., description="second run id"),
):
    result = service.compare_runs(a, b)
    if result is None:
        return error("one or both runs not found", 404)
    return result


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def view_run(run_id: int, request: Request):
    data = db.get_full_run(run_id)
    if not data:
        return HTMLResponse("Run not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "run.html",
        _run_review_context(data),
    )


@app.get("/runs/{run_id}/photos-grid", response_class=HTMLResponse)
def run_photos_grid(
    run_id: int,
    request: Request,
    sort: str = Query("keeper"),
    shot_type: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    min_keeper: Optional[float] = Query(None),
):
    data = db.get_full_run(run_id)
    if not data:
        return HTMLResponse("Run not found", status_code=404)
    ctx = _run_review_context(
        data,
        sort=sort,
        shot_type=shot_type,
        keyword=keyword,
        min_keeper=min_keeper,
    )
    return templates.TemplateResponse(request, "partials/photos_grid.html", ctx)


@app.patch(
    "/runs/{run_id}/photo/{photo_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_bearer)],
)
def patch_run_photo(run_id: int, photo_id: int, patch: PhotoPatch):
    if not any(
        value is not None
        for value in (
            patch.keywords,
            patch.keeper_score,
            patch.hero_potential,
            patch.shot_type,
            patch.promote_keywords,
        )
    ):
        return error("provide at least one field to update", 400)

    result = service.apply_photo_correction(
        run_id,
        photo_id,
        keywords=patch.keywords,
        keeper_score=patch.keeper_score,
        hero_potential=patch.hero_potential,
        shot_type=patch.shot_type,
        promote_keywords=patch.promote_keywords,
    )
    if result is None:
        return error("photo not found", 404)

    metrics.inc("photo_corrections")
    return {
        "ok": True,
        "run_id": run_id,
        "photo_id": photo_id,
        "photo": result["photo"],
        "client_id": result["client_id"],
        "prefs_updated": result["prefs_updated"],
    }


@app.get("/runs", response_class=JSONResponse)
def list_runs(include_archived: bool = Query(False)):
    return {
        "runs": [
            dict(row)
            for row in db.list_recent_runs(include_archived=include_archived)
        ]
    }


@app.post("/jobs", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
def create_job_endpoint(body: JobCreate):
    path, err = service.validate_job_create(folder=body.folder, callback_url=body.callback_url)
    if err:
        return error(err, 400 if "callback" in err else 404)
    assert path is not None

    source = body.source or service.source_label(path, client_id=body.client_id)
    model_name = body.model or config.VISION_MODEL
    if not config.QUEUE_ENABLED:
        result = service.analyze_folder_run(
            folder=path,
            source=source,
            model=model_name,
            limit=body.limit,
            project_id=body.project_id,
            write_sidecars=body.write_sidecars,
            sidecar_dir=body.sidecar_dir,
            client_id=body.client_id,
            recursive=body.recursive,
        )
        metrics.inc("analyze_folder")
        metrics.inc("photos_analyzed", result["count"])
        return {"status": "done", **result}

    return _enqueue_folder_job(
        path=path,
        source=source,
        model_name=model_name,
        limit=body.limit,
        write_sidecars=body.write_sidecars,
        sidecar_dir=body.sidecar_dir,
        project_id=body.project_id,
        client_id=body.client_id,
        callback_url=body.callback_url,
        recursive=body.recursive,
        extra={"client_id": body.client_id} if body.client_id else None,
    )


@app.get("/runs/{run_id}/manifest.json", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
def run_manifest(run_id: int, sidecar_dir: Optional[str] = Query(None)):
    manifest = service.build_run_manifest(run_id, sidecar_dir=sidecar_dir)
    if not manifest:
        return error("run not found", 404)
    return manifest


@app.post(
    "/runs/{run_id}/archive",
    response_class=JSONResponse,
    dependencies=[Depends(require_bearer)],
)
def archive_run_endpoint(run_id: int):
    if not db.archive_run(run_id):
        if db.get_run(run_id) is None:
            return error("run not found", 404)
        return error("run already archived", 409)
    metrics.inc("runs_archived")
    return {"ok": True, "run_id": run_id, "archived": True}


@app.get("/runs/{run_id}/export", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
def export_run(run_id: int):
    data = db.get_full_run(run_id)
    if not data:
        return error("run not found", 404)
    return data


@app.get("/runs/{run_id}/export.csv", dependencies=[Depends(require_bearer)])
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
def list_jobs_endpoint(limit: int = 20, status: Optional[str] = Query(None)):
    return {"jobs": [dict(row) for row in db.list_jobs(limit, status=status)]}


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
    parser.add_argument("--recursive", action="store_true", help="scan subfolders for images")
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
        print(f"Analyzing {path} (limit={limit}, recursive={args.recursive}) ...")
        result = service.analyze_folder_run(
            folder=path,
            source=source,
            limit=limit,
            project_id=str(args.mise_project_id) if args.mise_project_id else None,
            write_sidecars=args.write_sidecars,
            sidecar_dir=sidecar_dir,
            client_id=args.client_id,
            recursive=args.recursive,
        )
        total += result["count"]
        print(f"Run {result['run_id']} created with {result['count']} photos (source={source})")
        if args.write_sidecars:
            print(f"Sidecars written: {len(result['sidecars_written'])}")
    print(f"Done. Total photos: {total}")


if __name__ == "__main__":
    cli_main()
