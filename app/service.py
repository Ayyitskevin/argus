"""Shared analysis orchestration for API routes, queue jobs, and CLI."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import config, db, vision
from .sidecars import write_sidecar
from .callbacks import is_allowed_callback_url

log = logging.getLogger("argus.service")


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


def load_preferences(client_id: str | None) -> dict | None:
    """Load explicit prefs and merge history-derived nudges (Phase 4)."""
    if not client_id:
        return None

    prefs = dict(db.get_preferences(client_id) or {})
    stats = db.get_client_history_stats(client_id)

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
    data = db.get_full_run(run_id)
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
    if callback_url and not is_allowed_callback_url(callback_url):
        return None, "callback_url must be local or tailnet (http/https)"
    return path, None


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
    recursive: bool = False,
) -> dict:
    """Analyze and persist a folder synchronously."""
    model = model or config.VISION_MODEL
    prefs = load_preferences(client_id)
    analyses = vision.analyze_folder(
        folder,
        model=model,
        limit=limit,
        prefs=prefs,
        recursive=recursive,
    )
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
    out["recursive"] = recursive
    return out


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
    prefs = dict(db.get_preferences(client_id) or {})
    boosts = list(prefs.get("keyword_boosts") or [])
    for tag in promote_keywords or keywords or []:
        key = str(tag).strip()
        if not key:
            continue
        if key in boosts:
            boosts.remove(key)
        boosts.insert(0, key)
    prefs["keyword_boosts"] = boosts[:10]
    db.set_preferences(client_id, prefs)
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

    run = db.get_run(run_id)
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
    data_a = db.get_full_run(run_a_id)
    data_b = db.get_full_run(run_b_id)
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
