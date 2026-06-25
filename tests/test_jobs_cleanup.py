"""Job ledger cleanup — stale running reconciliation and ephemeral purge."""

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="argus-jobs-cleanup-")
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_VISION_BACKEND"] = "mock"

from app import db  # noqa: E402
from app.jobs import reconcile_on_startup, reconcile_stuck_jobs  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_jobs():
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM jobs")
    yield
    with db.tx() as con:
        con.execute("DELETE FROM jobs")


def test_reconcile_stale_running_marks_failed():
    job_id = db.create_job("/tmp/stale-folder", source="test", model="mock:test")
    db.update_job(job_id, status="running")
    count = db.reconcile_stale_running_jobs()
    assert count == 1
    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert "stale" in (job.get("error") or "").lower()


def test_reconcile_on_startup_requeues_all_running():
    fresh_id = db.create_job("/data/fresh", source="test", model="mock:test")
    db.update_job(fresh_id, status="running")
    assert reconcile_on_startup() == 1
    assert db.get_job(fresh_id)["status"] == "queued"
    assert db.get_job(fresh_id).get("error") is None


def test_reconcile_stuck_jobs_only_affects_old_running():
    fresh_id = db.create_job("/data/new-run", source="test", model="mock:test")
    db.update_job(fresh_id, status="running")
    old_id = db.create_job("/data/old-run", source="test", model="mock:test")
    db.update_job(old_id, status="running")
    with db.tx() as con:
        con.execute(
            "UPDATE jobs SET updated_at=datetime('now', '-45 minutes') WHERE id=?",
            (old_id,),
        )
    assert reconcile_stuck_jobs(max_age_minutes=30) == 1
    assert db.get_job(fresh_id)["status"] == "running"
    assert db.get_job(old_id)["status"] == "failed"


def test_purge_ephemeral_tmp_jobs():
    tmp_id = db.create_job("/tmp/ephemeral", source="test", model="mock:test")
    keep_id = db.create_job("/data/real-gallery", source="test", model="mock:test")
    removed = db.purge_jobs(folder_prefixes=("/tmp/",))
    assert removed == 1
    assert db.get_job(tmp_id) is None
    assert db.get_job(keep_id) is not None


def test_purge_failed_status_only():
    done_id = db.create_job("/data/a", source="test", model="mock:test")
    db.update_job(done_id, status="done")
    fail_id = db.create_job("/data/b", source="test", model="mock:test")
    db.update_job(fail_id, status="failed", error="boom")
    removed = db.purge_jobs(statuses=("failed", "dead_letter"))
    assert removed == 1
    assert db.get_job(done_id) is not None
    assert db.get_job(fail_id) is None