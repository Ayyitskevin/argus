"""Incremental folder jobs expose live progress (mock vision)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-job-progress-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "true"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db  # noqa: E402
from app.jobs import parse_job_progress, process_job  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", None)
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    monkeypatch.setattr(config, "VISION_BACKEND", "mock")
    db.init()
    with db.tx() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM photo_analyses")
        con.execute("DELETE FROM analysis_runs")


@pytest.fixture
def gallery_three() -> str:
    folder = Path(_TMP) / "gallery-three"
    folder.mkdir(exist_ok=True)
    for idx in range(3):
        Image.new("RGB", (400, 300), color=(40 + idx * 20, 60, 80)).save(
            folder / f"shot-{idx:02d}.jpg",
            format="JPEG",
        )
    return str(folder)


def test_incremental_run_updates_job_progress(gallery_three):
    job_id = db.create_job(gallery_three, limit=3, source="progress-test", model="mock:test")
    db.update_job(job_id, status="running")
    job = db.get_job(job_id)

    with patch.object(db, "update_job_progress", wraps=db.update_job_progress) as progress_mock:
        process_job(job)

    assert progress_mock.call_count >= 4
    final = db.get_job(job_id)
    assert final["status"] == "done"
    assert final["run_id"] is not None
    progress = parse_job_progress(final)
    assert progress is None or progress["done"] == progress["total"]


def test_ui_job_progress_partial_renders(gallery_three):
    job_id = db.create_job(gallery_three, limit=2, source="ui-progress", model="mock:test")
    db.update_job(job_id, status="running")
    db.update_job_progress(job_id, done=1, total=2, run_id=99, current_file="shot-00.jpg")

    page = client.get(f"/ui/jobs/{job_id}/progress")
    assert page.status_code == 200
    assert "1" in page.text and "2" in page.text
    assert "shot-00.jpg" in page.text
    assert "/runs/99" in page.text