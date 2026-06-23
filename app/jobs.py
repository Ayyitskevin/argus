"""Background job worker for queued Argus folder analysis."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, db
from . import service

log = logging.getLogger("argus.jobs")


def should_track_costs() -> bool:
    return config.CLOUD_BACKEND != "disabled"


def process_job(job: dict) -> None:
    """Run one claimed job and update its row with a terminal status."""
    job_id = job["id"]
    try:
        folder = Path(job["folder"]).expanduser().resolve()
        if not folder.is_dir():
            db.update_job(job_id, status="failed", error=f"folder not found: {job['folder']}")
            return

        result = service.analyze_folder_run(
            folder=folder,
            source=job.get("source") or str(folder),
            model=job.get("model") or config.VISION_MODEL,
            limit=job.get("limit_") or 20,
            project_id=job.get("project_id"),
            write_sidecars=bool(job.get("write_sidecars")),
            sidecar_dir=job.get("sidecar_dir"),
            client_id=job.get("client_id"),
        )

        job_result = {
            "run_id": result["run_id"],
            "count": result["count"],
            "sidecars_written": result["sidecars_written"]
            if job.get("write_sidecars")
            else None,
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
        log.info("Job %s completed -> run %s", job_id, result["run_id"])
    except Exception as exc:
        db.update_job(job_id, status="failed", error=str(exc))
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
                db.cleanup_old_jobs(days=1)

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
