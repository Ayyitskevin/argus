"""Browser UI flows — redirects, jobs pages, cookie auth (mock vision)."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-ui-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db  # noqa: E402
from app.auth import UI_TOKEN_COOKIE  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
TOKEN = "ui-test-token"


@pytest.fixture(autouse=True)
def _queue_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", None)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (800, 600), color=(90, 70, 50)).save(path, format="JPEG")
    return str(path)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", TOKEN)
    yield
    monkeypatch.setattr(config, "API_TOKEN", None)


def test_ui_analyze_folder_redirects_to_run(sample_image):
    folder = str(Path(sample_image).parent)
    r = client.post("/ui/analyze-folder", data={"folder": folder, "limit": 3}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/runs/")


def test_ui_analyze_upload_redirects(sample_image):
    with open(sample_image, "rb") as handle:
        r = client.post(
            "/ui/analyze",
            data={"client_id": "upload-test"},
            files={"file": ("test.jpg", handle, "image/jpeg")},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/runs/")


def test_ui_jobs_page_renders():
    r = client.get("/ui/jobs")
    assert r.status_code == 200
    assert "Job queue" in r.text
    assert 'href="/ui/jobs?status=failed"' in r.text


def test_ui_jobs_failed_filter_lists_job():
    job_id = db.create_job("/tmp/failed-ui", source="ui-failed", model="mock:test")
    db.update_job(job_id, status="failed", error="worker crash simulation")
    r = client.get("/ui/jobs?status=failed")
    assert r.status_code == 200
    assert "/tmp/failed-ui" in r.text
    assert "worker crash simulation" not in r.text  # error shown on detail page only


def test_ui_job_detail_after_queued_job(sample_image, monkeypatch):
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    folder = str(Path(sample_image).parent)
    job_id = db.create_job(folder, limit=2, source="ui-test", model="mock:test")
    r = client.get(f"/ui/jobs/{job_id}")
    assert r.status_code == 200
    assert job_id[:8] in r.text
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)


def test_ui_analyze_folder_redirects_to_job_when_queued(sample_image, monkeypatch):
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    folder = str(Path(sample_image).parent)
    r = client.post("/ui/analyze-folder", data={"folder": folder, "limit": 2}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/jobs/")
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)


def test_ui_requires_token_when_auth_enabled(sample_image, auth_on):
    folder = str(Path(sample_image).parent)
    r = client.post("/ui/analyze-folder", data={"folder": folder}, follow_redirects=False)
    assert r.status_code == 401


def test_ui_cookie_auth_allows_analyze(sample_image, auth_on):
    folder = str(Path(sample_image).parent)
    client.post("/ui/token", data={"api_token": TOKEN})
    r = client.post(
        "/ui/analyze-folder",
        data={"folder": folder, "limit": 1},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_bearer_cookie_works_on_api_route(sample_image, auth_on):
    client.cookies.set(UI_TOKEN_COOKIE, TOKEN)
    folder = str(Path(sample_image).parent)
    r = client.post("/analyze-folder", data={"folder": folder, "limit": 1})
    assert r.status_code == 200, r.text