"""Shared analysis orchestration for API routes, queue jobs, and CLI."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import config, db, vision
from .sidecars import write_sidecar

log = logging.getLogger("argus.service")


def result_to_dict(result: Any) -> dict:
    """Normalize Pydantic results and plain dicts into JSON-ready dicts."""
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


def load_preferences(client_id: str | None) -> dict | None:
    """Load client preferences and apply the simple history-derived bias."""
    if not client_id:
        return None

    prefs = dict(db.get_preferences(client_id) or {})
    stats = db.get_client_history_stats(client_id)
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
) -> dict:
    """Persist a batch of analyses and optionally write sidecars."""
    run_id = db.create_run(source=source, model=model, project_id=project_id)
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
) -> dict:
    """Analyze one local image path and persist it as a single-photo run."""
    model = model or config.VISION_MODEL
    prefs = load_preferences(client_id)
    analysis = vision.analyze_image(str(image_path), model=model, prefs=prefs)
    data = result_to_dict(analysis)
    if client_id:
        data["client_id"] = client_id

    run_id = db.create_run(
        source=source_label(image_path, client_id=client_id),
        model=model,
    )
    db.save_photo_analysis(run_id, data)
    db.set_run_photo_count(run_id, 1)
    data["run_id"] = run_id
    data["run_url"] = f"/runs/{run_id}"
    return data


def analyze_folder_run(
    *,
    folder: Path,
    source: str,
    model: str | None = None,
    limit: int | None = 20,
    project_id: str | None = None,
    write_sidecars: bool = False,
    sidecar_dir: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Analyze and persist a folder synchronously."""
    model = model or config.VISION_MODEL
    prefs = load_preferences(client_id)
    analyses = vision.analyze_folder(folder, model=model, limit=limit, prefs=prefs)
    out = persist_analysis_run(
        source=source,
        model=model,
        analyses=analyses,
        project_id=project_id,
        write_sidecars=write_sidecars,
        sidecar_dir=sidecar_dir,
    )
    if project_id:
        out["project_id"] = project_id
    if client_id:
        out["client_id"] = client_id
    return out
