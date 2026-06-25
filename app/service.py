"""Shared analysis orchestration for API routes, queue jobs, and CLI."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import config, db, metrics, metering, vision
from .auth_context import get_tenant_id
from .metering import MeteringError
from .sidecars import write_sidecar
from .callbacks import is_allowed_callback_url
from . import mise_dedup
from .folder_fingerprint import folder_fingerprint

log = logging.getLogger("argus.service")


def _tenant_dict(tenant_id: str | None) -> dict | None:
    if not tenant_id:
        return None
    return db.get_tenant(tenant_id)


def assert_path_within_media_roots(path: Path) -> None:
    """Confine analyzable local paths in SaaS mode so a tenant key cannot make
    the server read arbitrary files on the host. Homelab/non-SaaS is unrestricted
    (the operator's own machine). Raises AnalyzeError(403) on an out-of-bounds or
    traversal-escaped path; the resolved-path containment check stops '../'."""
    if not config.SAAS_MODE:
        return
    roots = config.ALLOWED_MEDIA_ROOTS
    if not roots:
        raise AnalyzeError("local path analysis is not permitted", 403)
    resolved = path.resolve()
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return
    raise AnalyzeError("path is outside the allowed media roots", 403)


def extract_client_id(source: str | None) -> str | None:
    """Parse ``client:<id>|...`` from a persisted run source label."""
    if not source or not source.startswith("client:"):
        return None
    rest = source[7:]
    pipe = rest.find("|")
    client_id = rest[:pipe] if pipe >= 0 else rest
    return client_id.strip() or None


def result_to_dict(result: Any) -> dict:
    """Normalize Pydantic results and plain dicts into JSON-ready dicts."""
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


def prefs_for_run(client_id: str | None, *, style: str | None = None) -> dict | None:
    """Load client prefs and optionally attach a vision style suffix key."""
    prefs = load_preferences(client_id)
    if not style:
        return prefs
    merged = dict(prefs or {})
    merged["style"] = style.strip()
    return merged


def load_preferences(client_id: str | None) -> dict | None:
    """Load explicit prefs and merge history-derived nudges (Phase 4)."""
    if not client_id:
        return None

    prefs = dict(db.get_preferences(client_id, tenant_id=db.GLOBAL_SCOPE) or {})
    stats = db.get_client_history_stats(client_id, tenant_id=db.GLOBAL_SCOPE)

    if not prefs.get("keyword_boosts") and stats.get("top_keywords"):
        prefs["keyword_boosts"] = stats["top_keywords"][:5]
    if not prefs.get("shot_type_preference") and stats.get("top_shot_type"):
        prefs["shot_type_preference"] = stats["top_shot_type"]

    bias = float(stats.get("bias", 0.0) or 0.0)
    if bias:
        prefs["culling_bias"] = float(prefs.get("culling_bias", 0.0) or 0.0) + bias
    return prefs


def simulated_cloud_cost(image_count: int) -> float:
    """Phase 2/4 cost accounting hook for stub/simulated cloud modes."""
    cost = image_count * config.CLOUD_COST_PER_IMAGE
    log.info(
        "[CLOUD STUB] Simulated cost for %s images: $%.4f",
        image_count,
        cost,
    )
    return cost


def resolve_mise_folder(
    *,
    folder: str | None = None,
    mise_gallery_id: int | None = None,
    mise_project_id: int | None = None,
) -> tuple[Path | None, dict, str | None]:
    """Resolve an explicit folder or a Mise gallery/project convention."""
    from . import mise_client

    mise_info: dict[str, int] = {}
    if mise_gallery_id is not None:
        mise_info["gallery_id"] = mise_gallery_id
    if mise_project_id is not None:
        mise_info["project_id"] = mise_project_id

    effective = folder
    if not effective and mise_gallery_id is not None and config.MISE_MEDIA_ROOT:
        effective = str(config.MISE_MEDIA_ROOT / str(mise_gallery_id) / "original")
    if not effective and mise_project_id is not None and config.MISE_MEDIA_ROOT:
        effective = str(config.MISE_MEDIA_ROOT / f"project-{mise_project_id}" / "original")
    if not effective and mise_gallery_id is not None and mise_client.is_enabled():
        try:
            row = mise_client.get_gallery(mise_gallery_id)
        except mise_client.MiseClientError:
            row = None
        if row and row.get("originals_path"):
            effective = str(row["originals_path"])
    if not effective:
        return None, mise_info, None

    path = Path(effective).expanduser().resolve()
    if not path.is_dir() and mise_gallery_id is not None:
        alt = path / "original" if path.name != "original" else path
        if alt.is_dir():
            path = alt

    return path, mise_info, effective


def source_label(path: Path, mise_info: dict | None = None, client_id: str | None = None) -> str:
    """Build the stored run source label while preserving the real folder path."""
    source = str(path)
    if client_id:
        source = f"client:{client_id}|{source}"
    if mise_info:
        marker = ",".join(f"{key}={value}" for key, value in mise_info.items())
        source = f"mise:{marker}|{source}"
    return source


def persist_analysis_run(
    *,
    source: str,
    model: str,
    analyses: list,
    project_id: str | None = None,
    write_sidecars: bool = False,
    sidecar_dir: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Persist a batch of analyses and optionally write sidecars."""
    run_id = db.create_run(
        source=source,
        model=model,
        project_id=project_id,
        tenant_id=tenant_id,
    )
    sidecars_written: list[str] = []
    photos: list[dict] = []

    for analysis in analyses:
        data = result_to_dict(analysis)
        db.save_photo_analysis(run_id, data)
        photos.append(data)
        if write_sidecars:
            written = write_sidecar(data["image_path"], data, sidecar_dir=sidecar_dir)
            sidecars_written.extend(str(path) for path in written.values())

    db.set_run_photo_count(run_id, len(photos))
    return {
        "run_id": run_id,
        "source": source,
        "model": model,
        "count": len(photos),
        "photos": photos,
        "sidecars_written": sidecars_written,
    }


def analyze_single_image(
    *,
    image_path: Path,
    model: str | None = None,
    client_id: str | None = None,
    tenant: dict | None = None,
) -> dict:
    """Analyze one local image path and persist it as a single-photo run."""
    model = model or config.VISION_MODEL
    tenant = tenant or (_tenant_dict(get_tenant_id()))
    tenant_id = tenant["id"] if tenant else None
    try:
        metering.enforce_caps(tenant_id, images=1)
    except MeteringError as exc:
        raise AnalyzeError(exc.message, exc.status_code) from exc
    prefs = load_preferences(client_id)
    try:
        analysis = vision.analyze_image(str(image_path), model=model, prefs=prefs, tenant=tenant)
    except MeteringError as exc:
        raise AnalyzeError(exc.message, exc.status_code) from exc
    if getattr(analysis, "analysis_failed", False):
        # The vision model errored; do not persist a fake "success" run.
        raise AnalyzeError(
            f"vision analysis failed: {analysis.culling.notes or 'model error'}", 502
        )
    data = result_to_dict(analysis)
    if client_id:
        data["client_id"] = client_id

    run_id = db.create_run(
        source=source_label(image_path, client_id=client_id),
        model=model,
        tenant_id=tenant_id,
    )
    db.save_photo_analysis(run_id, data)
    db.set_run_photo_count(run_id, 1)
    data["run_id"] = run_id
    data["run_url"] = f"/runs/{run_id}"
    return data


def sidecar_refs(image_path: str, sidecar_dir: str | Path | None = None) -> dict[str, str]:
    """Expected sidecar paths beside an image (DAM manifest helper)."""
    source = Path(image_path)
    out_dir = Path(sidecar_dir) if sidecar_dir else source.parent
    base = source.stem
    refs = {"argus": str(out_dir / f"{base}.argus.json")}
    refs["iptc"] = str(out_dir / f"{base}.iptc.json")
    refs["xmp"] = str(out_dir / f"{base}.xmp")
    return refs


def build_run_manifest(run_id: int, *, sidecar_dir: str | Path | None = None) -> dict | None:
    """DAM-friendly bundle: paths, scores, and expected sidecar refs."""
    data = db.get_full_run(run_id, tenant_id=db.GLOBAL_SCOPE)
    if not data:
        return None

    run = data["run"]
    entries = []
    for photo in data.get("photos", []):
        culling = photo.get("culling") or {}
        entries.append(
            {
                "id": photo.get("id"),
                "path": photo.get("image_path"),
                "basename": photo.get("basename"),
                "width": photo.get("width"),
                "height": photo.get("height"),
                "shot_type": photo.get("shot_type"),
                "keeper_score": culling.get("keeper_score"),
                "hero_potential": culling.get("hero_potential"),
                "technical_quality": culling.get("technical_quality"),
                "keywords": photo.get("keywords") or [],
                "alt_text": photo.get("alt_text"),
                "description": photo.get("description"),
                "suggested_iptc": photo.get("suggested_iptc") or {},
                "sidecars": sidecar_refs(photo["image_path"], sidecar_dir=sidecar_dir),
            }
        )

    return {
        "run_id": run["id"],
        "created_at": run.get("created_at"),
        "source": run.get("source"),
        "model": run.get("model"),
        "photo_count": len(entries),
        "client_id": extract_client_id(run.get("source")),
        "archived_at": run.get("archived_at"),
        "photos": entries,
    }


def queue_accepting_jobs() -> tuple[bool, str | None]:
    """Backpressure gate for new queued jobs (Phase 9)."""
    if db.queue_depth() >= config.MAX_QUEUE_DEPTH:
        return False, f"queue full ({config.MAX_QUEUE_DEPTH} queued jobs)"
    running = db.count_jobs_by_status("running")
    if running >= config.MAX_CONCURRENT_JOBS and db.queue_depth() >= config.MAX_CONCURRENT_JOBS:
        return False, "workers saturated — try again shortly"
    return True, None


def validate_job_create(
    *,
    folder: str,
    callback_url: str | None = None,
) -> tuple[Path | None, str | None]:
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        return None, f"folder not found or not a dir: {folder}"
    try:
        assert_path_within_media_roots(path)
    except AnalyzeError as exc:
        return None, exc.message
    if callback_url and not is_allowed_callback_url(callback_url):
        return None, "callback_url must be local or tailnet (http/https)"
    return path, None


def resolve_analyze_limit(
    limit: int | None,
    *,
    mise: bool = False,
) -> int | None:
    """Return max images to analyze, or None for the entire folder.

    Convention: ``0`` or negative means unlimited. ``None`` uses the configured
    default (``MISE_ARGUS_ANALYZE_LIMIT`` for Mise gallery paths, else
    ``DEFAULT_ANALYZE_LIMIT``).
    """
    if limit is None:
        limit = config.MISE_ARGUS_ANALYZE_LIMIT if mise else config.DEFAULT_ANALYZE_LIMIT
    if limit <= 0:
        return None
    return limit


def limit_for_storage(effective: int | None) -> int:
    """Persist ``0`` in the jobs table when the effective limit is unlimited."""
    return effective if effective is not None else 0


def estimate_for_job(job: dict) -> dict[str, Any] | None:
    """Best-effort preflight for a queued/running job row."""
    folder = job.get("folder")
    if not folder:
        return None
    path = Path(str(folder)).expanduser()
    if not path.is_dir():
        return None
    from . import mise_dedup

    limit_val = job.get("limit_")
    limit_arg: int | None
    if limit_val is None:
        limit_arg = None
    else:
        limit_arg = int(limit_val)
    return analyze_folder_estimate(
        path,
        limit=limit_arg,
        mise=mise_dedup.parse_mise_gallery_id(job.get("source")) is not None,
        recursive=bool(job.get("recursive")),
    )


def analyze_folder_estimate(
    folder: Path,
    *,
    limit: int | None = None,
    mise: bool = False,
    recursive: bool = False,
) -> dict[str, Any]:
    """Preflight image count and optional Grok cost estimate for homelab budgets."""
    effective_limit = resolve_analyze_limit(limit, mise=mise)
    images = _folder_image_list(folder, limit=effective_limit, recursive=recursive)
    count = len(images)
    estimate: dict[str, Any] = {
        "image_count": count,
        "analyze_all": effective_limit is None,
        "limit": limit_for_storage(effective_limit),
    }
    if config.VISION_BACKEND == "grok" and count > 0:
        from .xai_budget import today_snapshot

        cost_per = float(config.XAI_ESTIMATED_COST_PER_IMAGE)
        budget = today_snapshot()
        estimated_cost = round(count * cost_per, 4)
        estimate["estimated_cost_usd"] = estimated_cost
        estimate["cost_per_image_usd"] = cost_per
        estimate["budget"] = budget
        if budget.get("enabled") and budget.get("remaining_usd") is not None:
            estimate["budget_ok"] = estimated_cost <= float(budget["remaining_usd"])
    return estimate


def _photos_for_run(run_id: int) -> list[dict]:
    photos: list[dict] = []
    for row in db.get_photos_for_run(run_id):
        photo = db.get_photo_for_run(run_id, row["id"])
        if photo:
            photos.append(photo)
    return photos


def _folder_image_list(folder: Path, *, limit: int | None, recursive: bool) -> list[Path]:
    images = vision.collect_folder_images(folder, recursive=recursive)
    if limit is not None and limit > 0:
        images = images[:limit]
    return images


def _raise_if_all_failed(analyses: list) -> None:
    failed = [a for a in analyses if getattr(a, "analysis_failed", False)]
    if analyses and len(failed) == len(analyses):
        first_note = failed[0].culling.notes if failed else "model error"
        raise AnalyzeError(
            f"all {len(analyses)} image(s) failed vision analysis: {first_note}", 502
        )


def _folder_run_metadata(
    out: dict,
    *,
    failed_count: int,
    project_id: str | None,
    client_id: str | None,
    recursive: bool,
) -> dict:
    if failed_count:
        out["failed_count"] = failed_count
    if project_id:
        out["project_id"] = project_id
    if client_id:
        out["client_id"] = client_id
    out["recursive"] = recursive
    return out


def analyze_folder_run(
    *,
    folder: Path,
    source: str,
    model: str | None = None,
    limit: int | None = None,
    project_id: str | None = None,
    write_sidecars: bool = False,
    sidecar_dir: str | None = None,
    client_id: str | None = None,
    recursive: bool = False,
    tenant: dict | None = None,
    job_id: str | None = None,
    style: str | None = None,
) -> dict:
    """Analyze and persist a folder synchronously (incremental when job_id set)."""
    assert_path_within_media_roots(Path(folder))
    model = model or config.VISION_MODEL
    tenant = tenant or (_tenant_dict(get_tenant_id()))
    tenant_id = tenant["id"] if tenant else None
    effective_limit = resolve_analyze_limit(limit)
    images = _folder_image_list(folder, limit=effective_limit, recursive=recursive)
    if not images:
        raise AnalyzeError("no supported images found in folder", 404)
    planned = len(images)
    try:
        metering.enforce_caps(tenant_id, images=planned)
    except MeteringError as exc:
        raise AnalyzeError(exc.message, exc.status_code) from exc
    if config.VISION_BACKEND == "grok" and not config.SAAS_MODE and planned > 0:
        from .xai_budget import XaiBudgetError, check_budget

        try:
            check_budget(images=planned)
        except XaiBudgetError as exc:
            raise AnalyzeError(str(exc), 402) from exc

    prefs = prefs_for_run(client_id, style=style)

    if job_id and images:
        return _analyze_folder_incremental(
            folder=folder,
            source=source,
            model=model,
            images=images,
            project_id=project_id,
            write_sidecars=write_sidecars,
            sidecar_dir=sidecar_dir,
            client_id=client_id,
            recursive=recursive,
            tenant=tenant,
            tenant_id=tenant_id,
            job_id=job_id,
            prefs=prefs,
        )
    analyses = vision.analyze_folder(
        folder,
        model=model,
        limit=effective_limit,
        prefs=prefs,
        recursive=recursive,
        tenant=tenant,
    )
    _raise_if_all_failed(analyses)
    out = persist_analysis_run(
        source=source,
        model=model,
        analyses=analyses,
        project_id=project_id,
        write_sidecars=write_sidecars,
        sidecar_dir=sidecar_dir,
        tenant_id=tenant_id,
    )
    failed_count = sum(1 for a in analyses if getattr(a, "analysis_failed", False))
    return _folder_run_metadata(
        out,
        failed_count=failed_count,
        project_id=project_id,
        client_id=client_id,
        recursive=recursive,
    )


def _analyze_folder_incremental(
    *,
    folder: Path,
    source: str,
    model: str,
    images: list[Path],
    project_id: str | None,
    write_sidecars: bool,
    sidecar_dir: str | None,
    client_id: str | None,
    recursive: bool,
    tenant: dict | None,
    tenant_id: str | None,
    job_id: str,
    prefs: dict | None = None,
) -> dict:
    """Analyze one image at a time so queued jobs expose live progress."""
    import os

    prefs = prefs if prefs is not None else load_preferences(client_id)
    job_row = db.get_job(job_id, tenant_id=db.GLOBAL_SCOPE) or {}
    existing_run_id = job_row.get("run_id")
    done_basenames: set[str] = set()
    if existing_run_id:
        for row in db.get_photos_for_run(int(existing_run_id)):
            done_basenames.add(os.path.basename(str(row["image_path"])).lower())

    pending = [path for path in images if path.name.lower() not in done_basenames]
    total = len(images)
    done_count = len(done_basenames)

    if existing_run_id and not pending:
        photos = _photos_for_run(int(existing_run_id))
        return _folder_run_metadata(
            {
                "run_id": int(existing_run_id),
                "source": source,
                "model": model,
                "count": len(photos),
                "photos": photos,
                "sidecars_written": [],
                "resumed": True,
            },
            failed_count=0,
            project_id=project_id,
            client_id=client_id,
            recursive=recursive,
        )

    if existing_run_id:
        run_id = int(existing_run_id)
    else:
        run_id = db.create_run(
            source=source,
            model=model,
            project_id=project_id,
            tenant_id=tenant_id,
        )

    db.update_job_progress(job_id, done=done_count, total=total, run_id=run_id)

    analyses: list = []
    photos: list[dict] = []
    sidecars_written: list[str] = []

    if existing_run_id:
        photos.extend(_photos_for_run(run_id))

    batch_size = max(1, config.VISION_CONCURRENCY)
    processed = 0
    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        if batch:
            db.update_job_progress(
                job_id,
                done=done_count + processed,
                total=total,
                run_id=run_id,
                current_file=batch[0].name,
            )
        try:
            batch_results = vision.analyze_images_parallel(
                batch,
                model=model,
                prefs=prefs,
                tenant=tenant,
            )
        except Exception as exc:
            log.error("batch analyze failed at %s: %s", batch_start, exc)
            batch_results = []

        for image_path, analysis in zip(batch, batch_results):
            analyses.append(analysis)
            data = result_to_dict(analysis)
            db.save_photo_analysis(run_id, data)
            photos.append(data)
            if write_sidecars:
                written = write_sidecar(data["image_path"], data, sidecar_dir=sidecar_dir)
                sidecars_written.extend(str(path) for path in written.values())
            processed += 1
            db.set_run_photo_count(run_id, len(photos))
            db.update_job_progress(
                job_id,
                done=done_count + processed,
                total=total,
                run_id=run_id,
                current_file=image_path.name,
            )

    _raise_if_all_failed(analyses)
    failed_count = sum(1 for a in analyses if getattr(a, "analysis_failed", False))
    out = {
        "run_id": run_id,
        "source": source,
        "model": model,
        "count": len(photos),
        "photos": photos,
        "sidecars_written": sidecars_written,
    }
    return _folder_run_metadata(
        out,
        failed_count=failed_count,
        project_id=project_id,
        client_id=client_id,
        recursive=recursive,
    )


def sort_and_filter_photos(
    photos: list[dict],
    *,
    sort: str = "keeper",
    shot_type: str | None = None,
    keyword: str | None = None,
    min_keeper: float | None = None,
) -> list[dict]:
    """Apply Phase 7 culling UI sort/filter in memory."""
    filtered = list(photos)
    if shot_type:
        want = shot_type.strip().lower().replace(" ", "_")
        filtered = [photo for photo in filtered if photo.get("shot_type") == want]
    if keyword:
        needle = keyword.strip().lower()
        if needle:
            filtered = [
                photo
                for photo in filtered
                if any(needle in str(tag).lower() for tag in (photo.get("keywords") or []))
            ]
    if min_keeper is not None:
        filtered = [
            photo
            for photo in filtered
            if float((photo.get("culling") or {}).get("keeper_score", 0.0) or 0.0) >= min_keeper
        ]

    def keeper_score(photo: dict) -> float:
        return float((photo.get("culling") or {}).get("keeper_score", 0.0) or 0.0)

    def hero_score(photo: dict) -> float:
        return float((photo.get("culling") or {}).get("hero_potential", 0.0) or 0.0)

    if sort == "hero":
        filtered.sort(key=lambda photo: (-hero_score(photo), photo.get("basename", "")))
    elif sort == "name":
        filtered.sort(key=lambda photo: photo.get("basename", ""))
    else:
        filtered.sort(key=lambda photo: (-keeper_score(photo), photo.get("basename", "")))
    return filtered


def hero_candidates(photos: list[dict], limit: int = 5) -> list[dict]:
    """Top N photos by hero_potential for the hero strip."""
    return sort_and_filter_photos(photos, sort="hero")[:limit]


def record_correction_prefs(
    client_id: str,
    *,
    keywords: list[str] | None = None,
    promote_keywords: list[str] | None = None,
) -> dict:
    """Merge human corrections into explicit client prefs (beats history)."""
    prefs = dict(db.get_preferences(client_id, tenant_id=db.GLOBAL_SCOPE) or {})
    boosts = list(prefs.get("keyword_boosts") or [])
    for tag in promote_keywords or keywords or []:
        key = str(tag).strip()
        if not key:
            continue
        if key in boosts:
            boosts.remove(key)
        boosts.insert(0, key)
    prefs["keyword_boosts"] = boosts[:10]
    db.set_preferences(client_id, prefs, tenant_id=db.GLOBAL_SCOPE)
    return prefs


def apply_photo_correction(
    run_id: int,
    photo_id: int,
    *,
    keywords: list[str] | None = None,
    keeper_score: float | None = None,
    hero_potential: float | None = None,
    shot_type: str | None = None,
    promote_keywords: list[str] | None = None,
) -> dict | None:
    """PATCH one photo and optionally feed corrections into client prefs."""
    culling_patch: dict[str, float] = {}
    if keeper_score is not None:
        culling_patch["keeper_score"] = max(0.0, min(1.0, float(keeper_score)))
    if hero_potential is not None:
        culling_patch["hero_potential"] = max(0.0, min(1.0, float(hero_potential)))

    photo = db.update_photo_analysis(
        run_id,
        photo_id,
        keywords=keywords,
        culling=culling_patch or None,
        shot_type=shot_type,
    )
    if photo is None:
        return None

    run = db.get_run(run_id, tenant_id=db.GLOBAL_SCOPE)
    client_id = extract_client_id(run["source"] if run else None)
    prefs_updated = False
    if client_id and (keywords is not None or promote_keywords):
        record_correction_prefs(
            client_id,
            keywords=keywords,
            promote_keywords=promote_keywords,
        )
        prefs_updated = True

    return {
        "photo": photo,
        "client_id": client_id,
        "prefs_updated": prefs_updated,
    }


def compare_runs(run_a_id: int, run_b_id: int) -> dict | None:
    """Diff two runs by path overlap and score drift (re-analyze comparison)."""
    data_a = db.get_full_run(run_a_id, tenant_id=db.GLOBAL_SCOPE)
    data_b = db.get_full_run(run_b_id, tenant_id=db.GLOBAL_SCOPE)
    if not data_a or not data_b:
        return None

    def summarize(data: dict) -> dict:
        photos = data.get("photos") or []
        keepers = [
            float((photo.get("culling") or {}).get("keeper_score", 0.0) or 0.0)
            for photo in photos
        ]
        heroes = [
            float((photo.get("culling") or {}).get("hero_potential", 0.0) or 0.0)
            for photo in photos
        ]
        return {
            "run_id": data["run"]["id"],
            "photo_count": len(photos),
            "avg_keeper_score": round(sum(keepers) / len(keepers), 3) if keepers else None,
            "avg_hero_potential": round(sum(heroes) / len(heroes), 3) if heroes else None,
            "model": data["run"].get("model"),
            "source": data["run"].get("source"),
        }

    by_path_a = {photo["image_path"]: photo for photo in data_a.get("photos", [])}
    by_path_b = {photo["image_path"]: photo for photo in data_b.get("photos", [])}
    common_paths = sorted(set(by_path_a) & set(by_path_b))

    score_changes = []
    for path in common_paths:
        culling_a = by_path_a[path].get("culling") or {}
        culling_b = by_path_b[path].get("culling") or {}
        keeper_a = float(culling_a.get("keeper_score", 0.0) or 0.0)
        keeper_b = float(culling_b.get("keeper_score", 0.0) or 0.0)
        score_changes.append(
            {
                "image_path": path,
                "basename": by_path_a[path].get("basename"),
                "keeper_a": keeper_a,
                "keeper_b": keeper_b,
                "keeper_delta": round(keeper_b - keeper_a, 3),
            }
        )
    score_changes.sort(key=lambda item: abs(item["keeper_delta"]), reverse=True)

    summary_a = summarize(data_a)
    summary_b = summarize(data_b)
    keeper_a = summary_a.get("avg_keeper_score")
    keeper_b = summary_b.get("avg_keeper_score")
    return {
        "a": summary_a,
        "b": summary_b,
        "common_paths": len(common_paths),
        "only_in_a": len(set(by_path_a) - set(by_path_b)),
        "only_in_b": len(set(by_path_b) - set(by_path_a)),
        "avg_keeper_delta": round(keeper_b - keeper_a, 3)
        if keeper_a is not None and keeper_b is not None
        else None,
        "score_changes": score_changes[:50],
    }


class AnalyzeError(Exception):
    """User-facing analyze validation or queue errors."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def studio_run_urls(*, run_id: int | None = None, job_id: str | None = None) -> dict[str, str]:
    """Links for Mise admin — vision run review or queued job status."""
    base = config.PUBLIC_URL.rstrip("/")
    if run_id:
        return {"review_url": f"{base}/runs/{run_id}"}
    if job_id:
        return {"review_url": f"{base}/ui/jobs/{job_id}"}
    return {}


def perform_folder_analyze(
    *,
    folder: str | None = None,
    model: str | None = None,
    limit: int | None = None,
    write_sidecars: bool = False,
    sidecar_dir: str | None = None,
    mise_gallery_id: int | None = None,
    mise_project_id: int | None = None,
    client_id: str | None = None,
    recursive: bool = False,
    callback_url: str | None = None,
    tenant: dict | None = None,
    skip_dedup: bool = False,
) -> dict:
    """Shared folder analyze for JSON API and browser UI flows."""
    path, mise_info, attempted = resolve_mise_folder(
        folder=folder,
        mise_gallery_id=mise_gallery_id,
        mise_project_id=mise_project_id,
    )
    if path is None:
        raise AnalyzeError(
            "folder (or mise_gallery_id with ARGUS_MISE_MEDIA_ROOT or ARGUS_MISE_URL) required",
            400,
        )
    if not path.is_dir():
        raise AnalyzeError(f"folder not found or not a dir: {attempted}", 400)
    assert_path_within_media_roots(path)

    fp: str | None = None
    if mise_gallery_id is not None:
        fp = folder_fingerprint(path, recursive=recursive)

    if mise_gallery_id is not None and not skip_dedup:
        existing = mise_dedup.lookup(
            mise_gallery_id, client_id, folder_fingerprint=fp,
        )
        if existing:
            return existing

    if callback_url and not is_allowed_callback_url(callback_url):
        raise AnalyzeError("callback_url must be local or tailnet (http/https)", 400)

    tenant = tenant or _tenant_dict(get_tenant_id())
    project_id = str(mise_project_id) if mise_project_id is not None else None
    source = source_label(path, mise_info=mise_info, client_id=client_id)
    model_name = model or config.VISION_MODEL
    effective_limit = resolve_analyze_limit(limit, mise=mise_gallery_id is not None)
    stored_limit = limit_for_storage(effective_limit)
    estimate = analyze_folder_estimate(
        path,
        limit=limit,
        mise=mise_gallery_id is not None,
        recursive=recursive,
    )

    if config.QUEUE_ENABLED:
        ok, reason = queue_accepting_jobs()
        if not ok:
            raise AnalyzeError(reason or "queue saturated", 503)

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
            tenant_id=tenant["id"] if tenant else None,
        )
        if mise_gallery_id is not None:
            mise_dedup.record_queued(
                mise_gallery_id, client_id, job_id, folder_fingerprint=fp,
            )
        out: dict[str, Any] = {
            "mode": "queued",
            "job_id": job_id,
            "status": "queued",
            "source": source,
            "recursive": recursive,
            "limit": stored_limit,
            "analyze_all": effective_limit is None,
            "estimate": estimate,
        }
        if callback_url:
            out["callback_url"] = callback_url
        if mise_info:
            out["mise"] = mise_info
        if project_id:
            out["project_id"] = project_id
        if client_id:
            out["client_id"] = client_id
        out.update(studio_run_urls(job_id=job_id))
        return out

    result = analyze_folder_run(
        folder=path,
        source=source,
        model=model_name,
        limit=effective_limit,
        project_id=project_id,
        write_sidecars=write_sidecars,
        sidecar_dir=sidecar_dir,
        client_id=client_id,
        recursive=recursive,
        tenant=tenant,
    )
    metrics.inc("analyze_folder")
    metrics.inc("photos_analyzed", result["count"])
    if mise_info:
        result["mise"] = mise_info
    if not write_sidecars:
        result.pop("sidecars_written", None)
    elif sidecar_dir:
        result["sidecar_dir"] = sidecar_dir
    result["mode"] = "sync"
    result["estimate"] = estimate
    if mise_gallery_id is not None and result.get("run_id"):
        mise_dedup.record_done(
            mise_gallery_id, client_id, int(result["run_id"]), folder_fingerprint=fp,
        )
    if result.get("run_id"):
        result.update(studio_run_urls(run_id=int(result["run_id"])))
    return result
