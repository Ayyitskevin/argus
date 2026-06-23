"""Background job worker for queued Argus folder analysis."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, db, metrics, service
from .callbacks import fire_job_callback

log = logging.getLogger("argus.jobs")


def should_track_costs() -> bool:
    return config.CLOUD_BACKEND != "disabled"


def _notify(job_id: str, *, status: str, result: dict | None = None, error: str | None = None) -> None:
    job = db.get_job(job_id)
    if job:
        fire_job_callback(job, status=status, result=result, error=error)


def _fail_job(job_id: str, error_msg: str) -> None:
    job = db.get_job(job_id)
    retries = int((job or {}).get("retry_count") or 0)
    if retries < config.JOB_MAX_RETRIES:
        db.update_job(job_id, status="queued", retry_count=retries + 1, error=error_msg)
        metrics.inc("jobs_retried")
        log.warning("Job %s failed, requeued (retry %s): %s", job_id, retries + 1, error_msg)
        return

    db.update_job(job_id, status="dead_letter", error=error_msg)
    metrics.inc("jobs_dead_letter")
    metrics.inc("jobs_failed")
    log.error("Job %s moved to dead_letter: %s", job_id, error_msg)
    _notify(job_id, status="dead_letter", error=error_msg)


def process_job(job: dict) -> None:
    """Run one claimed job and update its row with a terminal status."""
    job_id = job["id"]
    try:
        folder = Path(job["folder"]).expanduser().resolve()
        if not folder.is_dir():
            _fail_job(job_id, f"folder not found: {job['folder']}")
            return

        tenant = None
        job_tenant_id = job.get("tenant_id")
        if job_tenant_id:
            tenant = db.get_tenant(job_tenant_id)

        result = service.analyze_folder_run(
            folder=folder,
            source=job.get("source") or str(folder),
            model=job.get("model") or config.VISION_MODEL,
            limit=job.get("limit_") or 20,
            project_id=job.get("project_id"),
            write_sidecars=bool(job.get("write_sidecars")),
            sidecar_dir=job.get("sidecar_dir"),
            client_id=job.get("client_id"),
            recursive=bool(job.get("recursive")),
            tenant=tenant,
        )

        job_result = {
            "run_id": result["run_id"],
            "count": result["count"],
            "sidecars_written": result["sidecars_written"]
            if job.get("write_sidecars")
            else None,
            "recursive": bool(job.get("recursive")),
        }
        if result.get("project_id"):
            job_result["project_id"] = result["project_id"]
        if job.get("client_id"):
            job_result["client_id"] = job["client_id"]
        if should_track_costs():
            job_result["simulated_cost"] = service.simulated_cloud_cost(result["count"])

        db.update_job(
            job_id,
            status="done",
            run_id=result["run_id"],
            result=job_result,
            error=None,
        )
        metrics.inc("jobs_completed")
        metrics.inc("photos_analyzed", result["count"])
        log.info("Job %s completed -> run %s", job_id, result["run_id"])
        _notify(job_id, status="done", result=job_result)
    except Exception as exc:
        _fail_job(job_id, str(exc))
        log.exception("Job %s failed", job_id)


class JobWorker:
    """Small thread-based queue runner suitable for local/tailnet dogfooding."""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._slots = threading.Semaphore(max(1, config.MAX_CONCURRENT_JOBS))
        self._cleanup_ticks = 0

    def start(self) -> None:
        if not config.QUEUE_ENABLED or self._thread is not None:
            return
        db.init()
        stale = db.reconcile_stale_running_jobs(max_age_minutes=30)
        if stale:
            log.warning("Reconciled %s stale running job(s) on worker start", stale)
        self._thread = threading.Thread(target=self._loop, name="argus-job-worker", daemon=True)
        self._thread.start()
        log.info("Queue worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._cleanup_ticks += 1
            if self._cleanup_ticks % 100 == 0:
                db.cleanup_old_jobs(days=config.JOB_RETENTION_DAYS)

            if self._slots.acquire(blocking=False):
                job = db.claim_next_job()
                if job is None:
                    self._slots.release()
                else:
                    threading.Thread(
                        target=self._run_claimed_job,
                        args=(job,),
                        daemon=True,
                    ).start()

            self._stop.wait(self.poll_interval)

    def _run_claimed_job(self, job: dict) -> None:
        try:
            process_job(job)
        finally:
            self._slots.release()