"""Background job worker for queued Argus folder analysis."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, db, metrics, mise_dedup, service
from .callbacks import fire_job_callback

log = logging.getLogger("argus.jobs")


def should_track_costs() -> bool:
    return config.CLOUD_BACKEND != "disabled"


RETRYABLE_STATUSES = frozenset({"failed", "dead_letter"})


def parse_job_progress(job: dict | None) -> dict | None:
    """Extract in-flight progress from a job row (``result.progress``)."""
    if not job:
        return None
    payload = job.get("result")
    if not isinstance(payload, dict):
        return None
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        return None
    total = int(progress.get("total") or 0)
    done = int(progress.get("done") or 0)
    if total <= 0:
        return None
    pct = min(100, int(round(100 * done / total)))
    return {
        "done": done,
        "total": total,
        "percent": pct,
        "current": progress.get("current"),
        "run_id": job.get("run_id") or progress.get("run_id"),
    }

# Jobs stuck in ``running`` longer than this are marked failed (worker still alive).
STUCK_JOB_MAX_AGE_MINUTES = 30


def reconcile_on_startup() -> int:
    """Re-queue jobs left ``running`` after a crash, deploy, or SIGKILL mid-job."""
    count = db.reconcile_stale_running_jobs(
        max_age_minutes=None,
        new_status="queued",
        error=None,
    )
    if count:
        metrics.inc("jobs_recovered", count)
    return count


def reconcile_stuck_jobs(
    *,
    max_age_minutes: int = STUCK_JOB_MAX_AGE_MINUTES,
) -> int:
    """Mark long-running jobs failed so they surface in /jobs?status=failed."""
    return db.reconcile_stale_running_jobs(
        max_age_minutes=max_age_minutes,
        new_status="failed",
        error="stale: exceeded max runtime without completion",
    )


def retry_job(job_id: str) -> dict:
    """Re-queue a terminal failed job for another worker pass."""
    job = db.get_job(job_id, tenant_id=db.GLOBAL_SCOPE)
    if not job:
        raise LookupError("job not found")
    if job["status"] not in RETRYABLE_STATUSES:
        raise ValueError(f"job status {job['status']} is not retryable")
    db.update_job(job_id, status="queued", error=None, retry_count=0)
    log.info("Job %s manually requeued", job_id)
    return {"ok": True, "job_id": job_id, "status": "queued"}


def _notify(job_id: str, *, status: str, result: dict | None = None, error: str | None = None) -> None:
    job = db.get_job(job_id, tenant_id=db.GLOBAL_SCOPE)
    if job:
        fire_job_callback(job, status=status, result=result, error=error)


def _fail_job(job_id: str, error_msg: str) -> None:
    job = db.get_job(job_id, tenant_id=db.GLOBAL_SCOPE)
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
    if job:
        gid = mise_dedup.parse_mise_gallery_id(job.get("source"))
        if gid is not None:
            from .folder_fingerprint import folder_fingerprint

            fp = None
            folder_raw = job.get("folder")
            if folder_raw:
                folder_path = Path(str(folder_raw)).expanduser()
                if folder_path.is_dir():
                    fp = folder_fingerprint(
                        folder_path, recursive=bool(job.get("recursive")),
                    )
            mise_dedup.record_failed(
                gid, job.get("client_id"), folder_fingerprint=fp,
            )
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
            limit=service.resolve_analyze_limit(job.get("limit_")),
            project_id=job.get("project_id"),
            write_sidecars=bool(job.get("write_sidecars")),
            sidecar_dir=job.get("sidecar_dir"),
            client_id=job.get("client_id"),
            recursive=bool(job.get("recursive")),
            tenant=tenant,
            job_id=job_id,
        )

        job_result = {
            "run_id": result["run_id"],
            "count": result["count"],
            "sidecars_written": result["sidecars_written"]
            if job.get("write_sidecars")
            else None,
            "recursive": bool(job.get("recursive")),
        }
        if result.get("failed_count"):
            job_result["failed_count"] = result["failed_count"]
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
        gid = mise_dedup.parse_mise_gallery_id(job.get("source"))
        if gid is not None:
            from .folder_fingerprint import folder_fingerprint

            fp = folder_fingerprint(folder, recursive=bool(job.get("recursive")))
            mise_dedup.record_done(
                gid, job.get("client_id"), int(result["run_id"]), folder_fingerprint=fp,
            )
            from . import plutus_client

            plutus_client.handoff_async(gid, int(result["run_id"]))
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
        recovered = reconcile_on_startup()
        if recovered:
            log.warning("Re-queued %s orphaned running job(s) after worker start", recovered)
        self._thread = threading.Thread(target=self._loop, name="argus-job-worker", daemon=True)
        self._thread.start()
        log.info("Queue worker started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._cleanup_ticks += 1
            if self._cleanup_ticks % 100 == 0:
                db.cleanup_old_jobs(days=config.JOB_RETENTION_DAYS)
                stuck = reconcile_stuck_jobs()
                if stuck:
                    log.warning("Marked %s stuck running job(s) as failed", stuck)

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