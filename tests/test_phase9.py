"""Phase 9 tests — auth gates, Prometheus, queue retry/DLQ, archive (mock only)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-phase9-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "true"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, metrics  # noqa: E402
from app.jobs import _fail_job, process_job  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer phase9-test-token"}


@pytest.fixture(autouse=True)
def phase9_env(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "phase9-test-token")
    monkeypatch.setattr(config, "JOB_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "MAX_QUEUE_DEPTH", 5)
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    db._SCHEMA_READY = False
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM jobs")


@pytest.fixture
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (1000, 700), color=(70, 90, 110)).save(path, format="JPEG")
    return str(path)


def test_export_requires_bearer_when_token_set(sample_image):
    run = client.post("/analyze", data={"path": sample_image}, headers=AUTH).json()
    denied = client.get(f"/runs/{run['run_id']}/export")
    assert denied.status_code == 401
    ok = client.get(f"/runs/{run['run_id']}/export", headers=AUTH)
    assert ok.status_code == 200


def test_prometheus_endpoint_gated(monkeypatch):
    monkeypatch.setattr(config, "PROMETHEUS_ENABLED", False)
    off = client.get("/metrics/prometheus")
    assert off.status_code == 404

    monkeypatch.setattr(config, "PROMETHEUS_ENABLED", True)
    on = client.get("/metrics/prometheus")
    assert on.status_code == 200
    assert "argus_photos_analyzed_total" in on.text
    assert metrics.prometheus_text().startswith("# HELP argus_uptime_seconds")


def test_job_retries_then_dead_letters():
    job_id = db.create_job("/no/such/folder", source="retry-test")
    job = db.get_job(job_id)
    process_job(job)
    first = db.get_job(job_id)
    assert first["status"] == "queued"
    assert first["retry_count"] == 1

    process_job(first)
    second = db.get_job(job_id)
    assert second["status"] == "dead_letter"

    listed = client.get("/jobs?status=dead_letter").json()["jobs"]
    assert any(row["id"] == job_id for row in listed)


def test_queue_backpressure_returns_503(monkeypatch, sample_image):
    monkeypatch.setattr(config, "MAX_QUEUE_DEPTH", 1)
    folder = str(Path(sample_image).parent)
    db.create_job(folder, source="fill-queue")

    resp = client.post(
        "/jobs",
        json={"folder": folder, "limit": 1},
        headers=AUTH,
    )
    assert resp.status_code == 503


def test_archive_run_hides_from_default_list(sample_image):
    run = client.post("/analyze", data={"path": sample_image}, headers=AUTH).json()
    run_id = run["run_id"]

    denied = client.post(f"/runs/{run_id}/archive")
    assert denied.status_code == 401

    ok = client.post(f"/runs/{run_id}/archive", headers=AUTH)
    assert ok.status_code == 200

    visible = [row["id"] for row in client.get("/runs").json()["runs"]]
    assert run_id not in visible

    archived = [
        row["id"]
        for row in client.get("/runs?include_archived=true").json()["runs"]
    ]
    assert run_id in archived


def test_fail_job_helper_increments_metrics():
    job_id = db.create_job("/tmp", source="metrics-test")
    before = metrics.snapshot()["counters"]["jobs_retried"]
    _fail_job(job_id, "synthetic failure")
    after = metrics.snapshot()["counters"]["jobs_retried"]
    assert after == before + 1