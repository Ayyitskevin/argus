"""Argus / photometa FastAPI application."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from urllib.parse import quote_plus
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import config, db, metrics, mise_client, pipeline, plutus_client, service
from . import mise_dedup
from .auth import UI_TOKEN_COOKIE, require_admin, require_bearer, resolve_auth, verify_api_access
from .auth_context import AuthContext
from . import metering, tenants
from .tenants import TenantError
from .callbacks import is_allowed_callback_url
from .jobs import JobWorker, parse_job_progress, retry_job
from .sidecars import write_sidecar
from .vision import make_thumbnail
from .vision_status import vision_status
from . import audit, billing, cap_alerts, health, rate_limit, saas, storage, structured_log, xai_budget
from .saas import assert_upload_only, get_full_run_for_ctx, get_job_for_ctx, tenant_scope

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
    limit: int | None = None
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
    saas.validate_saas_startup()
    db.init()
    worker = JobWorker()
    worker.start()
    app.state.job_worker = worker
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(
    title="argus / photometa",
    version="phase11",
    description=(
        "Vision metadata and culling API for photography workflows. "
        "SaaS tenants authenticate with `Authorization: Bearer argus_tk_<tenant>_<token>`. "
        "Homelab admin uses `ARGUS_API_TOKEN`."
    ),
    lifespan=lifespan,
)
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.middleware("http")(rate_limit.rate_limit_middleware)
app.middleware("http")(saas.saas_auth_middleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _request_auth(request: Request) -> AuthContext | None:
    return getattr(request.state, "auth", None)


def _ui_saas_auth(request: Request) -> AuthContext | None:
    """Resolve SaaS portal auth from middleware state or UI cookie."""
    ctx = _request_auth(request)
    if ctx is not None:
        return ctx
    try:
        return resolve_auth(request)
    except HTTPException:
        return None


def error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _enqueue_folder_job(
    *,
    path: Path,
    source: str,
    model_name: str,
    limit: int | None,
    write_sidecars: bool,
    sidecar_dir: str | None,
    project_id: str | None,
    client_id: str | None,
    callback_url: str | None,
    recursive: bool,
    tenant_id: str | None = None,
    extra: dict | None = None,
    mise: bool = False,
) -> JSONResponse:
    ok, reason = service.queue_accepting_jobs()
    if not ok:
        return error(reason or "queue saturated", 503)

    if callback_url and not is_allowed_callback_url(callback_url):
        return error("callback_url must be local or tailnet (http/https)", 400)

    effective_limit = service.resolve_analyze_limit(limit, mise=mise)
    stored_limit = service.limit_for_storage(effective_limit)
    job_id = db.create_job(
        str(path),
        stored_limit,
        write_sidecars,
        sidecar_dir,
        project_id=project_id,
        source=source,
        model=model_name,
        client_id=client_id,
        callback_url=callback_url,
        recursive=recursive,
        tenant_id=tenant_id,
    )
    response = {
        "job_id": job_id,
        "status": "queued",
        "source": source,
        "recursive": recursive,
        "limit": stored_limit,
        "analyze_all": effective_limit is None,
    }
    if callback_url:
        response["callback_url"] = callback_url
    if extra:
        response.update(extra)
    return JSONResponse(response)


@app.get("/healthz", tags=["ops"])
def healthz(request: Request):
    worker = getattr(request.app.state, "job_worker", None)
    report = health.build_health_report(worker=worker)
    body = {
        **report,
        "service_mode": config.SERVICE_MODE,
        "backend": config.VISION_BACKEND,
        "queue_enabled": config.QUEUE_ENABLED,
        "cloud_backend": config.CLOUD_BACKEND,
        "cloud_cost_per_image": config.CLOUD_COST_PER_IMAGE,
        "tailscale_hint": config.TAILSCALE_HINT,
        "auth_enabled": bool(config.API_TOKEN),
        "prometheus_enabled": config.PROMETHEUS_ENABLED,
        "model": config.VISION_MODEL,
        "grok_configured": bool(config.XAI_API_KEY),
        "xai_budget": xai_budget.today_snapshot()
        if config.VISION_BACKEND == "grok"
        else {"enabled": False},
        "vision_provider": "xai" if config.VISION_BACKEND == "grok" else config.VISION_BACKEND,
        "saas_mode": config.SAAS_MODE,
        "cloud_cost_cap_usd": config.CLOUD_COST_CAP_USD or None,
        "cloud_monthly_image_cap": config.CLOUD_MONTHLY_IMAGE_CAP or None,
        "tenant_count": len(db.list_tenants(active_only=True)) if config.SAAS_MODE else 0,
        "redis_rate_limits": bool(config.REDIS_URL),
        "cors_enabled": bool(config.CORS_ORIGINS),
    }
    status_code = 503 if report["status"] == "error" else 200
    return JSONResponse(body, status_code=status_code)


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


@app.get("/clients/{client_id}/history", response_class=JSONResponse)
def client_history(client_id: str, ctx: AuthContext = Depends(require_bearer)):
    return db.get_client_history_stats(client_id, tenant_id=tenant_scope(ctx))


@app.get("/mise/galleries", response_class=JSONResponse)
def list_mise_galleries(
    published: bool = Query(True),
    ctx: AuthContext = Depends(require_bearer),
):
    """Proxy Mise's published gallery index for operators and automation."""
    if not mise_client.is_enabled():
        return error("mise api not configured (ARGUS_MISE_URL + ARGUS_MISE_API_TOKEN)", 503)
    try:
        return mise_client.list_galleries(published=published)
    except mise_client.MiseClientError as exc:
        return error(str(exc), 502)


@app.get("/thumb/{photo_id}")
def get_thumb(photo_id: int, request: Request):
    ctx = _request_auth(request)
    image_path = db.get_photo_image_path(photo_id, tenant_id=tenant_scope(ctx))
    if not image_path:
        return error("photo not found", 404)

    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        return error("source image not found on disk", 404)

    try:
        thumb_bytes = make_thumbnail(path)
        return StreamingResponse(io.BytesIO(thumb_bytes), media_type="image/jpeg")
    except Exception:
        log.exception("failed to generate thumb for photo %s", photo_id)
        return error("failed to generate thumbnail", 500)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ctx = _request_auth(request)
    recent = [
        dict(row)
        for row in db.list_recent_runs(limit=6, tenant_id=tenant_scope(ctx))
    ]
    pipeline_snap = None
    if not config.SAAS_MODE and mise_client.is_enabled():
        try:
            pipeline_snap = pipeline.pipeline_snapshot()
        except Exception:
            log.exception("pipeline snapshot failed")
    return templates.TemplateResponse(
        request,
        "index.html",
        _ui_context(recent_runs=recent, pipeline=pipeline_snap),
    )


@app.post("/analyze", response_class=JSONResponse)
async def analyze_single(
    request: Request,
    file: Optional[UploadFile] = File(None),
    path: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    write_sidecar: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    ctx: AuthContext = Depends(require_bearer),
):
    try:
        assert_upload_only(ctx, has_file=file is not None, has_path=bool(path))
    except Exception as exc:
        if hasattr(exc, "status_code"):
            return error(exc.detail, exc.status_code)
        raise

    if not file and not path:
        return error("provide file or local path", 400)

    tmp_path: Path | None = None
    try:
        if file is not None:
            safe_name = Path(file.filename or "upload.jpg").name
            raw = await file.read()
            if ctx.tenant_id:
                stored = storage.save_tenant_upload(ctx.tenant_id, safe_name, raw)
                image_path = storage.resolve_upload_path(stored)
                tmp_path = image_path if not str(stored).startswith("s3://") else image_path
            else:
                tmp_path = config.DATA_DIR / f"upload_{uuid.uuid4().hex}_{safe_name}"
                tmp_path.write_bytes(raw)
                image_path = tmp_path
        else:
            image_path = Path(path or "").expanduser().resolve()
            if not image_path.is_file():
                return error(f"file not found: {path}", 404)
            try:
                service.assert_path_within_media_roots(image_path)
            except service.AnalyzeError as exc:
                return error(exc.message, exc.status_code)

        try:
            out = service.analyze_single_image(
                image_path=image_path,
                model=model,
                client_id=client_id,
                tenant=ctx.tenant,
            )
        except service.AnalyzeError as exc:
            return error(exc.message, exc.status_code)
        metrics.inc("analyze_single")
        metrics.inc("photos_analyzed")
        metrics.inc_tenant(ctx.tenant_id, "analyze_single")
        metrics.inc_tenant(ctx.tenant_id, "photos_analyzed")
        audit.record("analyze.single", request=request, ctx=ctx, status="ok", resource=str(image_path))
        structured_log.event(
            "analyze.single",
            tenant_id=ctx.tenant_id,
            run_id=out.get("run_id"),
            model=out.get("model"),
            path=str(image_path),
        )
        if ctx.tenant_id:
            cap_alerts.maybe_notify(ctx.tenant_id)
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


@app.post("/analyze-folder", response_class=JSONResponse)
def analyze_folder_endpoint(
    folder: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    limit: Optional[int] = Form(None),
    write_sidecars: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    mise_gallery_id: Optional[int] = Form(None),
    mise_project_id: Optional[int] = Form(None),
    client_id: Optional[str] = Form(None),
    recursive: bool = Form(False),
    callback_url: Optional[str] = Form(None),
    skip_dedup: bool = Form(False),
    ctx: AuthContext = Depends(require_bearer),
):
    try:
        assert_upload_only(
            ctx,
            has_file=False,
            has_path=bool(folder),
            has_folder=bool(folder or mise_gallery_id or mise_project_id),
        )
    except Exception as exc:
        if hasattr(exc, "status_code"):
            return error(exc.detail, exc.status_code)
        raise

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
            tenant=ctx.tenant,
            skip_dedup=skip_dedup,
        )
    except service.AnalyzeError as exc:
        return error(exc.message, exc.status_code)
    return _analyze_folder_response(result)


@app.post("/ui/analyze-folder")
def ui_analyze_folder(
    request: Request,
    folder: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    limit: Optional[int] = Form(None),
    write_sidecars: bool = Form(False),
    sidecar_dir: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    recursive: Optional[str] = Form(None),
    api_token: Optional[str] = Form(None),
):
    ctx = verify_api_access(request, form_token=api_token)
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
            tenant=ctx.tenant,
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
    ctx = verify_api_access(request, form_token=api_token)
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

        try:
            out = service.analyze_single_image(
                image_path=image_path,
                model=model,
                client_id=client_id,
                tenant=ctx.tenant,
            )
        except service.AnalyzeError as exc:
            return templates.TemplateResponse(
                request,
                "error.html",
                _ui_context(title="Analyze failed", message=exc.message, status_code=exc.status_code),
                status_code=exc.status_code,
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
    ctx = _request_auth(request)
    jobs = [
        dict(row)
        for row in db.list_jobs(limit, status=status, tenant_id=tenant_scope(ctx))
    ]
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
    ctx = _request_auth(request)
    job = get_job_for_ctx(job_id, ctx)
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
    progress = parse_job_progress(job)
    if progress and progress.get("run_id"):
        run_id = progress["run_id"]
    job_estimate = service.estimate_for_job(job)
    return templates.TemplateResponse(
        request,
        "job.html",
        _ui_context(
            job=job,
            job_result=result,
            run_id=run_id,
            progress=progress,
            job_estimate=job_estimate,
        ),
    )


@app.get("/ui/jobs/{job_id}/progress", response_class=HTMLResponse)
def ui_job_progress(request: Request, job_id: str):
    ctx = _request_auth(request)
    job = get_job_for_ctx(job_id, ctx)
    if not job:
        return HTMLResponse("Not found", status_code=404)
    progress = parse_job_progress(job)
    run_id = job.get("run_id") or (progress or {}).get("run_id")
    return templates.TemplateResponse(
        request,
        "partials/job_progress.html",
        _ui_context(job=job, progress=progress, run_id=run_id),
    )


@app.post("/ui/jobs/{job_id}/retry")
def ui_retry_job(request: Request, job_id: str):
    ctx = _request_auth(request)
    if get_job_for_ctx(job_id, ctx) is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(title="Not found", message=f"job not found: {job_id}", status_code=404),
            status_code=404,
        )
    try:
        retry_job(job_id)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(title="Retry failed", message=str(exc), status_code=400),
            status_code=400,
        )
    return RedirectResponse(f"/ui/jobs/{job_id}", status_code=303)


@app.post("/import/mise-project", response_class=JSONResponse, dependencies=[Depends(require_bearer)])
def import_mise_project(
    mise_project_id: int = Form(...),
    gallery_path: Optional[str] = Form(None),
    mise_gallery_id: Optional[int] = Form(None),
    limit: Optional[int] = Form(None),
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

    effective_limit = service.resolve_analyze_limit(limit, mise=True)
    stored_limit = service.limit_for_storage(effective_limit)

    if config.QUEUE_ENABLED:
        job_id = db.create_job(
            str(path),
            stored_limit,
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
            "limit": stored_limit,
            "analyze_all": effective_limit is None,
        }

    result = service.analyze_folder_run(
        folder=path,
        source=source,
        model=model_name,
        limit=effective_limit,
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
    mise_gallery_id = mise_dedup.parse_mise_gallery_id(run.get("source"))
    handoff = pipeline.gallery_handoff(mise_gallery_id) if mise_gallery_id else None
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
        photo_count=len(filtered),
        photo_total=len(photos),
        mise_gallery_id=mise_gallery_id,
        gallery_handoff=handoff,
        plutus_url=pipeline._public_plutus_url(),
        plutus_enabled=plutus_client.is_enabled(),
    )


@app.get("/ui/pipeline", response_class=HTMLResponse, tags=["ui"])
def ui_pipeline(request: Request):
    if config.SAAS_MODE:
        return RedirectResponse("/", status_code=303)
    try:
        snap = pipeline.pipeline_snapshot()
    except mise_client.MiseClientError as exc:
        return templates.TemplateResponse(
            request,
            "pipeline.html",
            _ui_context(
                title="Pipeline",
                checks={"mise": {"status": "error"}, "argus": {}, "plutus": {}},
                galleries=[],
                counts={"published": 0, "media_synced": 0, "argus_done": 0, "plutus_done": 0},
                urls={
                    "argus": pipeline._public_argus_url(),
                    "plutus": pipeline._public_plutus_url(),
                    "mise": config.MISE_URL,
                },
                handoff={
                    "mise_configured": mise_client.is_enabled(),
                    "plutus_auto": plutus_client.is_enabled(),
                },
                pipeline_error=str(exc),
            ),
        )
    msg = request.query_params.get("msg")
    err = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "pipeline.html",
        _ui_context(
            title="Pipeline",
            checks=snap["checks"],
            galleries=snap["galleries"],
            counts=snap["counts"],
            urls=snap["urls"],
            handoff=snap["handoff"],
            xai_budget=snap.get("xai_budget"),
            vision_concurrency=snap.get("vision_concurrency"),
            pipeline_message=msg,
            pipeline_error=err,
        ),
    )


@app.post("/ui/pipeline/analyze/{gallery_id}", tags=["ui"])
def ui_pipeline_analyze(gallery_id: int, request: Request, api_token: Optional[str] = Form(None)):
    if config.SAAS_MODE:
        return RedirectResponse("/", status_code=303)
    verify_api_access(request, form_token=api_token)
    try:
        result = service.perform_folder_analyze(
            mise_gallery_id=gallery_id,
            client_id="mise",
            limit=config.MISE_ARGUS_ANALYZE_LIMIT,
            skip_dedup=True,
        )
    except service.AnalyzeError as exc:
        return RedirectResponse(
            f"/ui/pipeline?error={quote_plus(exc.message)}",
            status_code=303,
        )
    if result.get("mode") == "queued":
        queued_msg = f"Queued job {result.get('job_id')}"
        return RedirectResponse(
            f"/ui/pipeline?msg={quote_plus(queued_msg)}",
            status_code=303,
        )
    return RedirectResponse(
        f"/runs/{result['run_id']}?msg={quote_plus('Analyze complete')}",
        status_code=303,
    )


@app.post("/ui/pipeline/plutus/{gallery_id}", tags=["ui"])
def ui_pipeline_plutus(gallery_id: int, request: Request, api_token: Optional[str] = Form(None)):
    if config.SAAS_MODE:
        return RedirectResponse("/", status_code=303)
    verify_api_access(request, form_token=api_token)
    handoff = pipeline.gallery_handoff(gallery_id)
    argus_run_id = (handoff or {}).get("argus_run_id")
    if not argus_run_id:
        return RedirectResponse(
            "/ui/pipeline?error=Argus+run+required+before+Plutus+upsell",
            status_code=303,
        )
    try:
        result = plutus_client.recommend_mise_gallery(gallery_id, argus_run_id=int(argus_run_id))
    except plutus_client.PlutusClientError as exc:
        mise_client.plutus_callback(gallery_id, status="error", error=str(exc))
        return RedirectResponse(
            f"/ui/pipeline?error={quote_plus(str(exc))}",
            status_code=303,
        )
    run_id = int(result.get("run_id") or 0)
    links = plutus_client.studio_links_for_run(run_id, result) if run_id else {}
    if run_id:
        mise_client.plutus_callback(
            gallery_id,
            run_id=run_id,
            status="done",
            **plutus_client.studio_handoff_fields(result),
        )
    bundle_n = result.get("bundle_count") or len(result.get("bundles") or [])
    plutus_msg = f"Plutus run {run_id} — {bundle_n} bundles"
    query = f"msg={quote_plus(plutus_msg)}"
    if links:
        query += f"&review_url={quote_plus(links['review_url'])}&pitch_url={quote_plus(links['pitch_url'])}"
    return RedirectResponse(f"/ui/pipeline?{query}", status_code=303)


@app.post("/ui/pipeline/run-all/{gallery_id}", tags=["ui"])
def ui_pipeline_run_all(gallery_id: int, request: Request, api_token: Optional[str] = Form(None)):
    if config.SAAS_MODE:
        return RedirectResponse("/", status_code=303)
    verify_api_access(request, form_token=api_token)
    try:
        result = pipeline.run_all(gallery_id)
    except pipeline.PipelineError as exc:
        return RedirectResponse(
            f"/ui/pipeline?error={quote_plus(exc.message)}",
            status_code=303,
        )
    msg = " · ".join(result.get("steps") or [])
    review_url = result.get("review_url") or ""
    pitch_url = result.get("pitch_url") or ""
    query = f"msg={quote_plus(msg)}"
    if review_url:
        query += f"&review_url={quote_plus(review_url)}"
    if pitch_url:
        query += f"&pitch_url={quote_plus(pitch_url)}"
    return RedirectResponse(f"/ui/pipeline?{query}", status_code=303)


@app.post("/ui/pipeline/offer/{gallery_id}", tags=["ui"])
def ui_pipeline_offer(gallery_id: int, request: Request, api_token: Optional[str] = Form(None)):
    """Retired — studio mode uses bundle review + pitch.txt, not storefront offers."""
    if config.SAAS_MODE:
        return RedirectResponse("/", status_code=303)
    verify_api_access(request, form_token=api_token)
    handoff = pipeline.gallery_handoff(gallery_id) or {}
    plutus_run_id = handoff.get("plutus_run_id")
    if not plutus_run_id:
        return RedirectResponse(
            "/ui/pipeline?error=Plutus+bundles+required+before+client+pitch",
            status_code=303,
        )
    links = plutus_client.studio_links_for_run(int(plutus_run_id))
    return RedirectResponse(
        "/ui/pipeline?"
        f"msg={quote_plus('Studio mode — review bundles and copy pitch.txt')}"
        f"&review_url={quote_plus(links['review_url'])}"
        f"&pitch_url={quote_plus(links['pitch_url'])}",
        status_code=303,
    )


@app.get("/ui/compare", response_class=HTMLResponse, tags=["ui"])
def ui_compare_runs(
    request: Request,
    a: Optional[int] = Query(None),
    b: Optional[int] = Query(None),
):
    ctx = _request_auth(request)
    recent = [
        dict(row)
        for row in db.list_recent_runs(limit=30, tenant_id=tenant_scope(ctx))
    ]
    compare_data = None
    compare_error = None
    if a is not None and b is not None:
        scope = tenant_scope(ctx)
        if scope and (
            db.get_run(a, tenant_id=scope) is None or db.get_run(b, tenant_id=scope) is None
        ):
            compare_error = "One or both runs not found (or not owned by this tenant)."
        else:
            compare_data = service.compare_runs(a, b)
            if compare_data is None:
                compare_error = "One or both runs not found."
    return templates.TemplateResponse(
        request,
        "compare_runs.html",
        _ui_context(
            title="Compare runs",
            recent_runs=recent,
            run_a=a,
            run_b=b,
            compare=compare_data,
            compare_error=compare_error,
        ),
    )


@app.get(
    "/runs/compare",
    response_class=JSONResponse,
    tags=["runs"],
    summary="Diff two runs by score drift on overlapping photos",
)
def compare_runs(
    request: Request,
    a: int = Query(..., description="first run id"),
    b: int = Query(..., description="second run id"),
):
    ctx = _request_auth(request)
    scope = tenant_scope(ctx)
    if scope and (
        db.get_run(a, tenant_id=scope) is None or db.get_run(b, tenant_id=scope) is None
    ):
        return error("one or both runs not found", 404)
    result = service.compare_runs(a, b)
    if result is None:
        return error("one or both runs not found", 404)
    return result


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def view_run(run_id: int, request: Request):
    ctx = _request_auth(request)
    data = get_full_run_for_ctx(run_id, ctx)
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
    auth_ctx = _request_auth(request)
    data = get_full_run_for_ctx(run_id, auth_ctx)
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
)
def patch_run_photo(
    run_id: int,
    photo_id: int,
    patch: PhotoPatch,
    ctx: AuthContext = Depends(require_bearer),
):
    if get_full_run_for_ctx(run_id, ctx) is None:
        return error("run not found", 404)
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
def list_runs(request: Request, include_archived: bool = Query(False)):
    ctx = _request_auth(request)
    return {
        "runs": [
            dict(row)
            for row in db.list_recent_runs(
                include_archived=include_archived,
                tenant_id=tenant_scope(ctx),
            )
        ]
    }


@app.post("/jobs", response_class=JSONResponse)
def create_job_endpoint(body: JobCreate, ctx: AuthContext = Depends(require_bearer)):
    if config.SAAS_MODE and not ctx.is_admin:
        return error("SaaS tenants cannot enqueue folder jobs; use POST /analyze with file upload", 403)
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
            tenant=ctx.tenant,
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
        tenant_id=ctx.tenant_id,
        extra={"client_id": body.client_id} if body.client_id else None,
    )


@app.get("/runs/{run_id}/manifest.json", response_class=JSONResponse)
def run_manifest(
    run_id: int,
    sidecar_dir: Optional[str] = Query(None),
    ctx: AuthContext = Depends(require_bearer),
):
    if get_full_run_for_ctx(run_id, ctx) is None:
        return error("run not found", 404)
    manifest = service.build_run_manifest(run_id, sidecar_dir=sidecar_dir)
    if not manifest:
        return error("run not found", 404)
    return manifest


@app.post(
    "/runs/{run_id}/archive",
    response_class=JSONResponse,
)
def archive_run_endpoint(run_id: int, ctx: AuthContext = Depends(require_bearer)):
    if get_full_run_for_ctx(run_id, ctx) is None:
        return error("run not found", 404)
    if not db.archive_run(run_id):
        if db.get_run(run_id, tenant_id=tenant_scope(ctx)) is None:
            return error("run not found", 404)
        return error("run already archived", 409)
    metrics.inc("runs_archived")
    return {"ok": True, "run_id": run_id, "archived": True}


@app.get("/runs/{run_id}/export", response_class=JSONResponse)
def export_run(run_id: int, ctx: AuthContext = Depends(require_bearer)):
    data = get_full_run_for_ctx(run_id, ctx)
    if not data:
        return error("run not found", 404)
    return data


@app.get("/runs/{run_id}/export.csv")
def export_run_csv(
    run_id: int,
    min_keeper: Optional[float] = Query(None, ge=0.0, le=1.0),
    ctx: AuthContext = Depends(require_bearer),
):
    data = get_full_run_for_ctx(run_id, ctx)
    if not data:
        return error("run not found", 404)

    photos = data.get("photos") or []
    if min_keeper is not None:
        photos = service.sort_and_filter_photos(photos, min_keeper=min_keeper)

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
    for photo in photos:
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
def photo_sidecar(run_id: int, photo_id: int, request: Request):
    ctx = _request_auth(request)
    data = get_full_run_for_ctx(run_id, ctx)
    if not data:
        return error("run not found", 404)
    for photo in data.get("photos", []):
        if photo.get("id") == photo_id:
            return photo
    return error("photo not found", 404)


@app.post(
    "/runs/{run_id}/write-sidecars",
    response_class=JSONResponse,
)
def write_sidecars_for_run(
    run_id: int,
    sidecar_dir: Optional[str] = Form(None),
    ctx: AuthContext = Depends(require_bearer),
):
    data = get_full_run_for_ctx(run_id, ctx)
    if not data:
        return error("run not found", 404)
    written = []
    for photo in data.get("photos", []):
        paths = write_sidecar(photo["image_path"], photo, sidecar_dir=sidecar_dir)
        written.extend(str(path) for path in paths.values())
    return {"run_id": run_id, "sidecars_written": written, "sidecar_dir": sidecar_dir}


@app.get("/jobs/costs", response_class=JSONResponse)
def get_costs(request: Request, summary: bool = False):
    ctx = _request_auth(request)
    costs = []
    total = 0.0
    by_project: dict[str, float] = {}
    for row in db.list_jobs(100, tenant_id=tenant_scope(ctx)):
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
def list_jobs_endpoint(request: Request, limit: int = 20, status: Optional[str] = Query(None)):
    ctx = _request_auth(request)
    return {
        "jobs": [
            dict(row)
            for row in db.list_jobs(limit, status=status, tenant_id=tenant_scope(ctx))
        ]
    }


@app.get("/jobs/{job_id}", response_class=JSONResponse)
def get_job(job_id: str, request: Request):
    ctx = _request_auth(request)
    job = get_job_for_ctx(job_id, ctx)
    if not job:
        return error("job not found", 404)
    return job


@app.post("/jobs/{job_id}/retry", response_class=JSONResponse)
def retry_job_endpoint(job_id: str, ctx: AuthContext = Depends(require_bearer)):
    if get_job_for_ctx(job_id, ctx) is None:
        return error("job not found", 404)
    try:
        return retry_job(job_id)
    except LookupError:
        return error("job not found", 404)
    except ValueError as exc:
        return error(str(exc), 400)


@app.get("/preferences", response_class=JSONResponse)
def get_prefs(
    client_id: Optional[str] = None,
    style: Optional[str] = None,
    ctx: AuthContext = Depends(require_bearer),
):
    scope = tenant_scope(ctx)
    return {
        "client_id": client_id,
        "style": style,
        "prefs": db.get_preferences(client_id, style, tenant_id=scope),
    }


@app.post("/preferences", response_class=JSONResponse)
def set_prefs(
    client_id: str = Form(...),
    prefs: str = Form(...),
    style: Optional[str] = Form(None),
    ctx: AuthContext = Depends(require_bearer),
):
    try:
        prefs_dict = json.loads(prefs)
    except Exception as exc:
        return error(f"invalid prefs json: {exc}", 400)
    pref_id = db.set_preferences(client_id, prefs_dict, style, tenant_id=tenant_scope(ctx))
    metrics.inc("preferences_writes")
    return {"ok": True, "id": pref_id, "client_id": client_id, "style": style, "prefs": prefs_dict}


class TenantCreate(BaseModel):
    id: str
    name: str
    vision_provider: str = "grok"
    cost_cap_usd: float | None = None
    monthly_image_cap: int | None = None


class TenantPatch(BaseModel):
    name: str | None = None
    active: bool | None = None
    vision_provider: str | None = None
    cost_cap_usd: float | None = None
    monthly_image_cap: int | None = None


class TenantKeyCreate(BaseModel):
    label: str | None = None


@app.get("/saas/status", response_class=JSONResponse)
def saas_status():
    return {
        "saas_mode": config.SAAS_MODE,
        "cloud_backend": config.CLOUD_BACKEND,
        "default_vision_provider": config.DEFAULT_VISION_PROVIDER,
        "providers": ["grok", "openai", "anthropic"],
        "storage_backend": config.STORAGE_BACKEND,
        "rate_limit_enabled": config.RATE_LIMIT_ENABLED,
        "audit_log_enabled": config.AUDIT_LOG_ENABLED,
        "billing_enabled": billing.billing_enabled(),
        "billing": billing.billing_status(),
        "metering": metering.usage_snapshot(),
    }


@app.get("/saas/billing/status", response_class=JSONResponse)
def saas_billing_status():
    return billing.billing_status()


@app.get(
    "/tenant/profile",
    response_class=JSONResponse,
    dependencies=[Depends(require_bearer)],
    tags=["tenant"],
    summary="Tenant metadata for the authenticated API key",
)
def tenant_profile(ctx: AuthContext = Depends(require_bearer)):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    return {"tenant": ctx.tenant}


@app.get(
    "/tenant/usage",
    response_class=JSONResponse,
    dependencies=[Depends(require_bearer)],
    tags=["tenant"],
    summary="Current-period usage, caps, and soft cap warnings",
)
def tenant_usage(ctx: AuthContext = Depends(require_bearer)):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    return metering.usage_snapshot(ctx.tenant_id)


@app.get(
    "/tenant/keys",
    response_class=JSONResponse,
    dependencies=[Depends(require_bearer)],
    tags=["tenant"],
    summary="List API keys for the authenticated tenant (metadata only)",
)
def tenant_list_keys(ctx: AuthContext = Depends(require_bearer)):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    keys = []
    for row in db.list_tenant_keys(ctx.tenant_id):
        item = dict(row)
        item["is_current"] = item["id"] == ctx.api_key_id
        keys.append(item)
    return {"keys": keys}


@app.post(
    "/tenant/keys",
    response_class=JSONResponse,
    tags=["tenant"],
    summary="Issue a new API key for the authenticated tenant",
)
def tenant_issue_key(
    request: Request,
    body: TenantKeyCreate,
    ctx: AuthContext = Depends(require_bearer),
):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    try:
        issued = tenants.issue_api_key(ctx.tenant_id, label=body.label)
    except TenantError as exc:
        return error(str(exc), 400)
    audit.record(
        "tenant.key.issue",
        request=request,
        ctx=ctx,
        tenant_id=ctx.tenant_id,
        resource=issued["key_id"],
    )
    return issued


@app.delete(
    "/tenant/keys/{key_id}",
    response_class=JSONResponse,
    tags=["tenant"],
    summary="Revoke an API key owned by the authenticated tenant",
)
def tenant_revoke_key(key_id: str, request: Request, ctx: AuthContext = Depends(require_bearer)):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    keys = {row["id"] for row in db.list_tenant_keys(ctx.tenant_id) if not row.get("revoked_at")}
    if key_id not in keys:
        return error("key not found", 404)
    if key_id == ctx.api_key_id:
        return error("cannot revoke the API key used for this request", 400)
    if len(keys) <= 1:
        return error("cannot revoke your only active API key", 400)
    if not tenants.revoke_key(key_id):
        return error("key already revoked", 409)
    audit.record(
        "tenant.key.revoke",
        request=request,
        ctx=ctx,
        tenant_id=ctx.tenant_id,
        resource=key_id,
    )
    return {"ok": True, "key_id": key_id, "revoked": True}


@app.get("/admin/tenants", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_list_tenants(active_only: bool = Query(False)):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    return {"tenants": db.list_tenants(active_only=active_only)}


@app.post("/admin/tenants", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_create_tenant(request: Request, body: TenantCreate, ctx: AuthContext = Depends(require_admin)):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    try:
        tenant = tenants.create_tenant(
            body.id,
            name=body.name,
            vision_provider=body.vision_provider,
            cost_cap_usd=body.cost_cap_usd,
            monthly_image_cap=body.monthly_image_cap,
        )
    except TenantError as exc:
        audit.record("admin.tenant.create", request=request, ctx=ctx, status="error", detail=str(exc))
        return error(str(exc), 400)
    audit.record("admin.tenant.create", request=request, ctx=ctx, tenant_id=tenant["id"], resource=tenant["id"])
    return {"tenant": tenant}


@app.patch("/admin/tenants/{tenant_id}", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_patch_tenant(tenant_id: str, body: TenantPatch):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    tenant = db.update_tenant(tenant_id, **body.model_dump(exclude_unset=True))
    if not tenant:
        return error("tenant not found", 404)
    return {"tenant": tenant}


@app.get("/admin/tenants/{tenant_id}/keys", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_list_tenant_keys(tenant_id: str):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    if not db.get_tenant(tenant_id):
        return error("tenant not found", 404)
    return {"keys": db.list_tenant_keys(tenant_id)}


@app.delete(
    "/admin/tenants/{tenant_id}/keys/{key_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_admin)],
)
def admin_revoke_tenant_key(tenant_id: str, key_id: str):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    if not db.get_tenant(tenant_id):
        return error("tenant not found", 404)
    keys = {row["id"] for row in db.list_tenant_keys(tenant_id)}
    if key_id not in keys:
        return error("key not found", 404)
    if not tenants.revoke_key(key_id):
        return error("key already revoked", 409)
    return {"ok": True, "key_id": key_id, "revoked": True}


@app.post("/admin/tenants/{tenant_id}/keys", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_issue_tenant_key(tenant_id: str, body: TenantKeyCreate):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    try:
        issued = tenants.issue_api_key(tenant_id, label=body.label)
    except TenantError as exc:
        return error(str(exc), 400)
    return issued


@app.get("/admin/tenants/{tenant_id}/usage", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_tenant_usage(tenant_id: str):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    if not db.get_tenant(tenant_id):
        return error("tenant not found", 404)
    return metering.usage_snapshot(tenant_id)


@app.get("/admin/audit", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_audit_log(
    tenant_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    return {"events": db.list_audit_events(tenant_id=tenant_id, action=action, limit=limit)}


@app.post("/admin/tenants/{tenant_id}/billing/checkout", response_class=JSONResponse, dependencies=[Depends(require_admin)])
def admin_billing_checkout(tenant_id: str, request: Request, ctx: AuthContext = Depends(require_admin)):
    if not config.SAAS_MODE:
        return error("ARGUS_SAAS_MODE is disabled", 404)
    try:
        session = billing.create_checkout_session(tenant_id)
    except billing.BillingError as exc:
        return error(str(exc), 400)
    audit.record("billing.checkout", request=request, ctx=ctx, tenant_id=tenant_id, detail=session)
    return session


@app.post("/tenant/billing/checkout", response_class=JSONResponse)
def tenant_billing_checkout(request: Request, ctx: AuthContext = Depends(require_bearer)):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    try:
        session = billing.create_checkout_session(ctx.tenant_id)
    except billing.BillingError as exc:
        return error(str(exc), 400)
    audit.record("billing.checkout", request=request, ctx=ctx, tenant_id=ctx.tenant_id, detail=session)
    return session


@app.post("/tenant/billing/portal", response_class=JSONResponse)
def tenant_billing_portal(request: Request, ctx: AuthContext = Depends(require_bearer)):
    if not ctx.tenant:
        return error("tenant API key required", 403)
    try:
        portal = billing.create_billing_portal_session(ctx.tenant_id)
    except billing.BillingError as exc:
        return error(str(exc), 400)
    audit.record("billing.portal", request=request, ctx=ctx, tenant_id=ctx.tenant_id)
    return portal


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not billing.verify_webhook_signature(payload, sig):
        return error("invalid stripe signature", 400)
    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return error("invalid json", 400)
    billing.handle_webhook_event(event)
    audit.record("billing.webhook", request=request, detail={"type": event.get("type")})
    return {"received": True}


@app.get("/ui/saas", response_class=HTMLResponse)
def ui_saas_landing(request: Request):
    if not config.SAAS_MODE:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "saas_landing.html",
        _ui_context(
            title="Argus Cloud",
            billing_enabled=billing.billing_enabled(),
        ),
    )


@app.get("/ui/saas/login", response_class=HTMLResponse)
def ui_saas_login(request: Request):
    if not config.SAAS_MODE:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "saas_login.html", _ui_context(title="Sign in"))


@app.post("/ui/saas/login")
def ui_saas_login_post(request: Request, api_token: str = Form(...)):
    if not config.SAAS_MODE:
        return RedirectResponse("/", status_code=302)
    try:
        ctx = resolve_auth(request, form_token=api_token)
    except HTTPException:
        return templates.TemplateResponse(
            request,
            "saas_login.html",
            _ui_context(title="Sign in", login_error="Invalid API key or admin token"),
            status_code=401,
        )
    dest = "/ui/saas/app/admin" if ctx.is_admin else "/ui/saas/app"
    response = RedirectResponse(dest, status_code=303)
    response.set_cookie(UI_TOKEN_COOKIE, api_token.strip(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


def _tenant_ui_redirect(request: Request) -> AuthContext | RedirectResponse:
    ctx = _ui_saas_auth(request)
    if ctx is None or ctx.is_admin or not ctx.tenant:
        return RedirectResponse("/ui/saas/login", status_code=303)
    return ctx


@app.get("/ui/saas/app", response_class=HTMLResponse)
def ui_saas_tenant_app(request: Request):
    ctx = _tenant_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    usage = metering.usage_snapshot(ctx.tenant_id)
    recent = [
        dict(row)
        for row in db.list_recent_runs(limit=8, tenant_id=ctx.tenant_id)
    ]
    tenant_jobs = [dict(row) for row in db.list_jobs(limit=10, tenant_id=ctx.tenant_id)]
    audit_events = db.list_audit_events(tenant_id=ctx.tenant_id, limit=15)
    tenant_keys = []
    for row in db.list_tenant_keys(ctx.tenant_id):
        item = dict(row)
        item["is_current"] = item["id"] == ctx.api_key_id
        tenant_keys.append(item)
    return templates.TemplateResponse(
        request,
        "saas_dashboard.html",
        _ui_context(
            title="Tenant dashboard",
            portal_mode="tenant",
            tenant=ctx.tenant,
            usage=usage,
            cap_warnings=usage.get("warnings") or [],
            recent_runs=recent,
            tenant_jobs=tenant_jobs,
            tenant_keys=tenant_keys,
            current_key_id=ctx.api_key_id,
            audit_events=audit_events,
            billing_enabled=billing.billing_enabled(),
            tenant_message="API key revoked." if request.query_params.get("keys_updated") else None,
            tenant_error=request.query_params.get("keys_error"),
        ),
    )


def _admin_ui_redirect(request: Request) -> AuthContext | RedirectResponse:
    ctx = _ui_saas_auth(request)
    if ctx is None or not ctx.is_admin:
        return RedirectResponse("/ui/saas/login", status_code=303)
    return ctx


def _admin_tenant_context(
    request: Request,
    tenant_id: str,
    *,
    admin_message: str | None = None,
    admin_error: str | None = None,
    issued_api_key: str | None = None,
) -> dict:
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _ui_context(
        title=f"Tenant {tenant_id}",
        tenant=tenant,
        usage=metering.usage_snapshot(tenant_id),
        keys=db.list_tenant_keys(tenant_id),
        billing_enabled=billing.billing_enabled(),
        admin_message=admin_message,
        admin_error=admin_error,
        issued_api_key=issued_api_key,
    )


@app.post("/ui/saas/app/keys")
def ui_saas_tenant_issue_key(
    request: Request,
    label: Optional[str] = Form(None),
):
    ctx = _tenant_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    try:
        issued = tenants.issue_api_key(ctx.tenant_id, label=label.strip() if label else None)
    except TenantError as exc:
        return RedirectResponse(
            f"/ui/saas/app?keys_error={quote_plus(str(exc))}",
            status_code=303,
        )
    audit.record(
        "tenant.key.issue",
        request=request,
        ctx=ctx,
        tenant_id=ctx.tenant_id,
        resource=issued["key_id"],
    )
    usage = metering.usage_snapshot(ctx.tenant_id)
    recent = [dict(row) for row in db.list_recent_runs(limit=8, tenant_id=ctx.tenant_id)]
    tenant_jobs = [dict(row) for row in db.list_jobs(limit=10, tenant_id=ctx.tenant_id)]
    tenant_keys = []
    for row in db.list_tenant_keys(ctx.tenant_id):
        item = dict(row)
        item["is_current"] = item["id"] == ctx.api_key_id
        tenant_keys.append(item)
    return templates.TemplateResponse(
        request,
        "saas_dashboard.html",
        _ui_context(
            title="Tenant dashboard",
            portal_mode="tenant",
            tenant=ctx.tenant,
            usage=usage,
            cap_warnings=usage.get("warnings") or [],
            recent_runs=recent,
            tenant_jobs=tenant_jobs,
            tenant_keys=tenant_keys,
            current_key_id=ctx.api_key_id,
            audit_events=db.list_audit_events(tenant_id=ctx.tenant_id, limit=15),
            billing_enabled=billing.billing_enabled(),
            issued_api_key=issued["api_key"],
        ),
    )


@app.post("/ui/saas/app/keys/{key_id}/revoke")
def ui_saas_tenant_revoke_key(request: Request, key_id: str):
    ctx = _tenant_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    active = {
        row["id"]
        for row in db.list_tenant_keys(ctx.tenant_id)
        if not row.get("revoked_at")
    }
    if key_id not in active:
        return RedirectResponse("/ui/saas/app?keys_error=key+not+found", status_code=303)
    if key_id == ctx.api_key_id:
        return RedirectResponse(
            "/ui/saas/app?keys_error=cannot+revoke+the+key+used+for+this+session",
            status_code=303,
        )
    if len(active) <= 1:
        return RedirectResponse("/ui/saas/app?keys_error=cannot+revoke+your+only+active+key", status_code=303)
    if not tenants.revoke_key(key_id):
        return RedirectResponse("/ui/saas/app?keys_error=key+already+revoked", status_code=303)
    audit.record(
        "tenant.key.revoke",
        request=request,
        ctx=ctx,
        tenant_id=ctx.tenant_id,
        resource=key_id,
    )
    return RedirectResponse("/ui/saas/app?keys_updated=1", status_code=303)


@app.get("/ui/saas/app/admin", response_class=HTMLResponse)
def ui_saas_admin_app(
    request: Request,
    created: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    audit_tenant: Optional[str] = Query(None),
    audit_action: Optional[str] = Query(None),
):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    tenant_rows = db.list_tenants()
    global_usage = db.global_usage_totals()
    audit_events = db.list_audit_events(
        tenant_id=audit_tenant or None,
        action=audit_action or None,
        limit=50,
    )
    admin_message = f"Tenant {created} created." if created else None
    admin_error = error
    return templates.TemplateResponse(
        request,
        "saas_dashboard.html",
        _ui_context(
            title="Admin console",
            portal_mode="admin",
            tenants=tenant_rows,
            global_usage=global_usage,
            audit_events=audit_events,
            audit_tenant=audit_tenant,
            audit_action=audit_action,
            billing_enabled=billing.billing_enabled(),
            admin_message=admin_message,
            admin_error=admin_error,
        ),
    )


@app.get("/ui/saas/app/admin/tenants/{tenant_id}", response_class=HTMLResponse)
def ui_saas_admin_tenant(
    request: Request,
    tenant_id: str,
    updated: Optional[str] = Query(None),
    revoked: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    if not db.get_tenant(tenant_id):
        return RedirectResponse("/ui/saas/app/admin?error=tenant+not+found", status_code=303)
    admin_message = None
    if updated:
        admin_message = "Settings saved."
    elif revoked:
        admin_message = "API key revoked."
    return templates.TemplateResponse(
        request,
        "saas_admin_tenant.html",
        _admin_tenant_context(
            request,
            tenant_id,
            admin_message=admin_message,
            admin_error=error,
        ),
    )


@app.post("/ui/saas/app/admin/tenants")
def ui_saas_admin_create_tenant(
    request: Request,
    tenant_id: str = Form(...),
    name: str = Form(...),
    vision_provider: str = Form("grok"),
    monthly_image_cap: Optional[str] = Form(None),
    cost_cap_usd: Optional[str] = Form(None),
):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    if not config.SAAS_MODE:
        return RedirectResponse("/ui/saas/app/admin?error=SaaS+mode+disabled", status_code=303)
    cap_images = int(monthly_image_cap) if monthly_image_cap and monthly_image_cap.strip() else None
    cap_cost = float(cost_cap_usd) if cost_cap_usd and cost_cap_usd.strip() else None
    try:
        tenant = tenants.create_tenant(
            tenant_id,
            name=name,
            vision_provider=vision_provider,
            cost_cap_usd=cap_cost,
            monthly_image_cap=cap_images,
        )
    except TenantError as exc:
        audit.record("admin.tenant.create", request=request, ctx=ctx, status="error", detail=str(exc))
        return RedirectResponse(
            f"/ui/saas/app/admin?error={quote_plus(str(exc))}",
            status_code=303,
        )
    audit.record("admin.tenant.create", request=request, ctx=ctx, tenant_id=tenant["id"], resource=tenant["id"])
    return RedirectResponse(f"/ui/saas/app/admin/tenants/{tenant['id']}", status_code=303)


@app.post("/ui/saas/app/admin/tenants/{tenant_id}")
def ui_saas_admin_patch_tenant(
    request: Request,
    tenant_id: str,
    name: Optional[str] = Form(None),
    active: str = Form("1"),
    vision_provider: Optional[str] = Form(None),
    monthly_image_cap: Optional[str] = Form(None),
    cost_cap_usd: Optional[str] = Form(None),
):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    if not config.SAAS_MODE or not db.get_tenant(tenant_id):
        return RedirectResponse("/ui/saas/app/admin?error=tenant+not+found", status_code=303)
    fields: dict = {"active": active.strip() in {"1", "true", "yes", "on"}}
    if name is not None and name.strip():
        fields["name"] = name.strip()
    if vision_provider and vision_provider.strip():
        fields["vision_provider"] = vision_provider.strip()
    if monthly_image_cap is not None:
        stripped = monthly_image_cap.strip()
        fields["monthly_image_cap"] = int(stripped) if stripped else None
    if cost_cap_usd is not None:
        stripped = cost_cap_usd.strip()
        fields["cost_cap_usd"] = float(stripped) if stripped else None
    db.update_tenant(tenant_id, **fields)
    audit.record("admin.tenant.patch", request=request, ctx=ctx, tenant_id=tenant_id, detail=fields)
    return RedirectResponse(f"/ui/saas/app/admin/tenants/{tenant_id}?updated=1", status_code=303)


@app.post("/ui/saas/app/admin/tenants/{tenant_id}/keys")
def ui_saas_admin_issue_key(
    request: Request,
    tenant_id: str,
    label: Optional[str] = Form(None),
):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    if not config.SAAS_MODE or not db.get_tenant(tenant_id):
        return RedirectResponse("/ui/saas/app/admin?error=tenant+not+found", status_code=303)
    try:
        issued = tenants.issue_api_key(tenant_id, label=label.strip() if label else None)
    except TenantError as exc:
        return templates.TemplateResponse(
            request,
            "saas_admin_tenant.html",
            _admin_tenant_context(request, tenant_id, admin_error=str(exc)),
            status_code=400,
        )
    audit.record(
        "admin.tenant.key.issue",
        request=request,
        ctx=ctx,
        tenant_id=tenant_id,
        resource=issued["key_id"],
    )
    return templates.TemplateResponse(
        request,
        "saas_admin_tenant.html",
        _admin_tenant_context(request, tenant_id, issued_api_key=issued["api_key"]),
    )


@app.post("/ui/saas/app/admin/tenants/{tenant_id}/keys/{key_id}/revoke")
def ui_saas_admin_revoke_key(request: Request, tenant_id: str, key_id: str):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    if not config.SAAS_MODE or not db.get_tenant(tenant_id):
        return RedirectResponse("/ui/saas/app/admin?error=tenant+not+found", status_code=303)
    keys = {row["id"] for row in db.list_tenant_keys(tenant_id)}
    if key_id not in keys:
        return RedirectResponse(
            f"/ui/saas/app/admin/tenants/{tenant_id}?error=key+not+found",
            status_code=303,
        )
    if not tenants.revoke_key(key_id):
        return RedirectResponse(
            f"/ui/saas/app/admin/tenants/{tenant_id}?error=key+already+revoked",
            status_code=303,
        )
    audit.record("admin.tenant.key.revoke", request=request, ctx=ctx, tenant_id=tenant_id, resource=key_id)
    return RedirectResponse(f"/ui/saas/app/admin/tenants/{tenant_id}?revoked=1", status_code=303)


@app.post("/ui/saas/app/admin/tenants/{tenant_id}/billing/checkout")
def ui_saas_admin_billing_checkout(request: Request, tenant_id: str):
    ctx = _admin_ui_redirect(request)
    if not isinstance(ctx, AuthContext):
        return ctx
    if not config.SAAS_MODE or not db.get_tenant(tenant_id):
        return RedirectResponse("/ui/saas/app/admin?error=tenant+not+found", status_code=303)
    try:
        session = billing.create_checkout_session(tenant_id)
    except billing.BillingError as exc:
        return templates.TemplateResponse(
            request,
            "saas_admin_tenant.html",
            _admin_tenant_context(request, tenant_id, admin_error=str(exc)),
            status_code=400,
        )
    audit.record("billing.checkout", request=request, ctx=ctx, tenant_id=tenant_id, detail=session)
    return RedirectResponse(session["checkout_url"], status_code=303)


@app.get("/ui/saas/billing", response_class=HTMLResponse)
def ui_saas_billing(request: Request, success: Optional[str] = Query(None), cancelled: Optional[str] = Query(None)):
    ctx = _ui_saas_auth(request)
    if ctx is None:
        return RedirectResponse("/ui/saas/login", status_code=303)
    tenant = ctx.tenant if ctx.tenant else None
    return templates.TemplateResponse(
        request,
        "saas_billing.html",
        _ui_context(
            title="Billing",
            tenant=tenant,
            billing_success=bool(success),
            billing_cancelled=bool(cancelled),
            billing_enabled=billing.billing_enabled(),
            billing_info=billing.billing_status(),
            billing_error=None,
        ),
    )


@app.post("/ui/saas/billing/checkout")
def ui_saas_billing_checkout(request: Request, api_token: Optional[str] = Form(None)):
    ctx = verify_api_access(request, form_token=api_token)
    if not ctx.tenant:
        return error("tenant API key required", 403)
    try:
        session = billing.create_checkout_session(ctx.tenant_id)
    except billing.BillingError as exc:
        return templates.TemplateResponse(
            request,
            "saas_billing.html",
            _ui_context(title="Billing", tenant=ctx.tenant, billing_error=str(exc), billing_enabled=False),
            status_code=400,
        )
    audit.record("billing.checkout", request=request, ctx=ctx, tenant_id=ctx.tenant_id, detail=session)
    return RedirectResponse(session["checkout_url"], status_code=303)


@app.post("/ui/saas/billing/portal")
def ui_saas_billing_portal(request: Request, api_token: Optional[str] = Form(None)):
    ctx = verify_api_access(request, form_token=api_token)
    if not ctx.tenant:
        return error("tenant API key required", 403)
    try:
        portal = billing.create_billing_portal_session(ctx.tenant_id)
    except billing.BillingError as exc:
        return templates.TemplateResponse(
            request,
            "saas_billing.html",
            _ui_context(title="Billing", tenant=ctx.tenant, billing_error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(portal["portal_url"], status_code=303)


@app.post("/ui/saas/analyze")
async def ui_saas_analyze(
    request: Request,
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    api_token: Optional[str] = Form(None),
):
    ctx = verify_api_access(request, form_token=api_token)
    if not ctx.tenant:
        return RedirectResponse("/ui/saas/login", status_code=303)
    safe_name = Path(file.filename or "upload.jpg").name
    raw = await file.read()
    stored = storage.save_tenant_upload(ctx.tenant_id, safe_name, raw)
    image_path = storage.resolve_upload_path(stored)
    try:
        out = service.analyze_single_image(
            image_path=image_path,
            model=model,
            tenant=ctx.tenant,
        )
    except service.AnalyzeError as exc:
        audit.record("analyze.single", request=request, ctx=ctx, status="error", detail=exc.message)
        return templates.TemplateResponse(
            request,
            "error.html",
            _ui_context(title="Analyze failed", message=exc.message, status_code=exc.status_code),
            status_code=exc.status_code,
        )
    metrics.inc_tenant(ctx.tenant_id, "analyze_single")
    audit.record("analyze.single", request=request, ctx=ctx, resource=str(image_path), detail={"run_id": out["run_id"]})
    structured_log.event(
        "analyze.single",
        tenant_id=ctx.tenant_id,
        run_id=out["run_id"],
        path=str(image_path),
        source="ui",
    )
    cap_alerts.maybe_notify(ctx.tenant_id)
    return RedirectResponse(f"/runs/{out['run_id']}", status_code=303)


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
