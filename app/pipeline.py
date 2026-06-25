"""Homelab pipeline status — Mise galleries → Argus vision → Plutus bundles."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from . import config, db, health, mise_client, plutus_client, service

log = logging.getLogger("argus.pipeline")


class PipelineError(Exception):
    """Operator-facing pipeline orchestration failure."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _public_plutus_url() -> str:
    return config.PLUTUS_PUBLIC_URL or config.PLUTUS_URL or "http://127.0.0.1:8030"


def _public_argus_url() -> str:
    if config.PUBLIC_URL:
        return config.PUBLIC_URL.rstrip("/")
    hint = (config.TAILSCALE_HINT or "127.0.0.1").lower()
    return f"http://{hint}:{config.PORT}"


def service_checks() -> dict[str, Any]:
    report = health.build_health_report(worker=None)
    return {
        "overall": report.get("status"),
        "argus": report.get("checks", {}).get("vision", {}),
        "mise": report.get("checks", {}).get("mise", {}),
        "plutus": report.get("checks", {}).get("plutus", {}),
        "queue": report.get("checks", {}).get("queue", {}),
    }


def gallery_rows(*, published: bool = True) -> list[dict[str, Any]]:
    if not mise_client.is_enabled():
        return []
    body = mise_client.list_galleries(published=published)
    rows: list[dict[str, Any]] = []
    for g in body.get("galleries") or []:
        gid = int(g["id"])
        media = None
        if config.MISE_MEDIA_ROOT:
            media_dir = config.MISE_MEDIA_ROOT / str(gid) / "original"
            if media_dir.is_dir():
                media = len(list(media_dir.glob("*")))
        rows.append(
            {
                "id": gid,
                "title": g.get("title") or f"Gallery {gid}",
                "slug": g.get("slug"),
                "published": bool(g.get("published")),
                "argus_run_id": g.get("argus_last_run_id"),
                "argus_status": g.get("argus_last_status"),
                "argus_at": g.get("argus_last_at"),
                "argus_error": g.get("argus_last_error"),
                "plutus_run_id": g.get("plutus_last_run_id"),
                "plutus_status": g.get("plutus_last_status"),
                "plutus_at": g.get("plutus_last_at"),
                "plutus_error": g.get("plutus_last_error"),
                "local_media_count": media,
            }
        )
    return rows


def pipeline_snapshot() -> dict[str, Any]:
    checks = service_checks()
    galleries = gallery_rows()
    synced = sum(1 for g in galleries if (g.get("local_media_count") or 0) > 0)
    vision_done = sum(1 for g in galleries if g.get("argus_status") == "done")
    plutus_done = sum(1 for g in galleries if g.get("plutus_status") == "done")
    return {
        "checks": checks,
        "galleries": galleries,
        "counts": {
            "published": len(galleries),
            "media_synced": synced,
            "argus_done": vision_done,
            "plutus_done": plutus_done,
        },
        "urls": {
            "argus": _public_argus_url(),
            "plutus": _public_plutus_url(),
            "mise": config.MISE_URL or "",
        },
        "handoff": {
            "mise_configured": mise_client.is_enabled(),
            "plutus_auto": plutus_client.is_enabled(),
            "plutus_mise_admin": bool(config.PLUTUS_URL),
        },
    }


def gallery_handoff(gallery_id: int) -> dict[str, Any] | None:
    for row in gallery_rows(published=False):
        if row["id"] == gallery_id:
            return row
    return None


def _wait_job(job_id: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = db.get_job(job_id, tenant_id=db.GLOBAL_SCOPE)
        if job and job.get("status") in ("done", "failed", "dead_letter"):
            return job
        time.sleep(2)
    raise PipelineError(f"vision job {job_id} did not finish within {int(timeout)}s")


def _wait_mise_plutus(gallery_id: int, *, timeout: float = 90) -> int | None:
    """Allow background handoff_async to record plutus_last_* on Mise."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = gallery_handoff(gallery_id) or {}
        if row.get("plutus_status") == "done" and row.get("plutus_run_id"):
            return int(row["plutus_run_id"])
        time.sleep(2)
    return None


def run_all(gallery_id: int, *, vision_limit: int | None = None) -> dict[str, Any]:
    """Vision (if needed) → Plutus bundles → review + pitch links (studio mode)."""
    if not plutus_client.is_enabled():
        raise PipelineError("Plutus is not configured (ARGUS_PLUTUS_URL + token)")

    handoff = gallery_handoff(gallery_id)
    if not handoff:
        raise PipelineError(f"gallery {gallery_id} not found in Mise")

    steps: list[str] = []
    argus_run_id = handoff.get("argus_run_id")
    argus_status = handoff.get("argus_status")
    vision_ran = False
    limit = service.resolve_analyze_limit(
        vision_limit,
        mise=True,
    )

    if argus_status != "done" or not argus_run_id:
        if not (handoff.get("local_media_count") or 0):
            raise PipelineError("gallery media not synced locally — check ARGUS_MISE_MEDIA_ROOT")
        try:
            result = service.perform_folder_analyze(
                mise_gallery_id=gallery_id,
                client_id="mise",
                limit=limit,
                skip_dedup=True,
            )
        except service.AnalyzeError as exc:
            raise PipelineError(exc.message) from exc

        vision_ran = True
        if result.get("mode") == "queued":
            job_id = result.get("job_id")
            if not job_id:
                raise PipelineError("vision job was queued but no job_id returned")
            steps.append(f"vision queued {job_id[:8]}")
            job = _wait_job(job_id, timeout=float(config.PIPELINE_RUN_ALL_TIMEOUT))
            if job.get("status") != "done":
                err = job.get("error") or job.get("status")
                raise PipelineError(f"vision failed: {err}")
            argus_run_id = int(job["run_id"])
        else:
            argus_run_id = int(result["run_id"])
        steps.append(f"vision run {argus_run_id}")
    else:
        steps.append(f"vision skipped (run {argus_run_id})")

    plutus_run_id: int | None = None
    plutus_result: dict | None = None
    if vision_ran:
        plutus_run_id = _wait_mise_plutus(gallery_id)

    if not plutus_run_id:
        handoff = gallery_handoff(gallery_id) or {}
        if not vision_ran and handoff.get("plutus_status") == "done" and handoff.get("plutus_run_id"):
            plutus_run_id = int(handoff["plutus_run_id"])
            steps.append(f"bundles skipped (run {plutus_run_id})")
        else:
            try:
                plutus_result = plutus_client.recommend_mise_gallery(
                    gallery_id, argus_run_id=int(argus_run_id)
                )
            except plutus_client.PlutusClientError as exc:
                mise_client.plutus_callback(gallery_id, status="error", error=str(exc))
                raise PipelineError(str(exc)) from exc
            plutus_run_id = int(plutus_result["run_id"])
            bundle_n = plutus_result.get("bundle_count") or len(
                plutus_result.get("bundles") or []
            )
            steps.append(f"bundles run {plutus_run_id} ({bundle_n} bundles)")
    else:
        steps.append(f"bundles run {plutus_run_id} (auto)")

    links = plutus_client.studio_links_for_run(plutus_run_id, plutus_result)
    callback_fields = (
        plutus_client.studio_handoff_fields(plutus_result)
        if plutus_result
        else links
    )
    mise_client.plutus_callback(
        gallery_id,
        run_id=plutus_run_id,
        status="done",
        **callback_fields,
    )
    steps.append("review + pitch ready")

    return {
        "gallery_id": gallery_id,
        "argus_run_id": argus_run_id,
        "plutus_run_id": plutus_run_id,
        "review_url": links["review_url"],
        "pitch_url": links["pitch_url"],
        "steps": steps,
    }