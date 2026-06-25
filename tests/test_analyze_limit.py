"""Folder analyze limit semantics: 0 = unlimited, None = configured default."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-limit-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, service  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer limit-test-token"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "limit-test-token")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "DEFAULT_ANALYZE_LIMIT", 20)
    monkeypatch.setattr(config, "MISE_ARGUS_ANALYZE_LIMIT", 0)
    db._SCHEMA_READY = False
    db.init()
    set_auth_context(None)
    yield
    set_auth_context(None)


def test_resolve_analyze_limit_zero_is_unlimited():
    assert service.resolve_analyze_limit(0) is None
    assert service.resolve_analyze_limit(-1) is None


def test_resolve_analyze_limit_none_uses_defaults(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_ANALYZE_LIMIT", 20)
    monkeypatch.setattr(config, "MISE_ARGUS_ANALYZE_LIMIT", 0)
    assert service.resolve_analyze_limit(None) == 20
    assert service.resolve_analyze_limit(None, mise=True) is None


def test_limit_for_storage_maps_unlimited_to_zero():
    assert service.limit_for_storage(None) == 0
    assert service.limit_for_storage(5) == 5


def test_analyze_folder_zero_processes_all_images(tmp_path):
    folder = tmp_path / "gallery"
    folder.mkdir()
    for i in range(4):
        Image.new("RGB", (20, 20), (i * 10, 0, 0)).save(folder / f"img{i}.jpg")

    result = service.analyze_folder_run(
        folder=folder,
        source="limit-test",
        limit=0,
    )
    assert result["count"] == 4


def test_mise_queue_job_stores_zero_for_unlimited(monkeypatch, tmp_path):
    folder = tmp_path / "mise" / "7" / "original"
    folder.mkdir(parents=True)
    Image.new("RGB", (20, 20), (1, 2, 3)).save(folder / "a.jpg")
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)

    with patch("app.service.resolve_mise_folder") as resolve:
        resolve.return_value = (folder.resolve(), {"gallery_id": 7}, str(folder))
        with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
            with patch("app.db.create_job", return_value="job-all") as create_job:
                out = service.perform_folder_analyze(mise_gallery_id=7, limit=0)

    assert out["analyze_all"] is True
    assert out["limit"] == 0
    create_job.assert_called_once()
    assert create_job.call_args[0][1] == 0


def test_enqueue_folder_job_preserves_zero_limit(monkeypatch, tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)

    with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
        with patch("app.db.create_job", return_value="job-zero") as create_job:
            from app.main import _enqueue_folder_job

            resp = _enqueue_folder_job(
                path=folder,
                source="enqueue-test",
                model_name="mock:test",
                limit=0,
                write_sidecars=False,
                sidecar_dir=None,
                project_id=None,
                client_id=None,
                callback_url=None,
                recursive=False,
            )

    payload = resp.body.decode()
    assert '"analyze_all":true' in payload.replace(" ", "")
    create_job.assert_called_once()
    assert create_job.call_args[0][1] == 0


def test_analyze_folder_endpoint_mise_default_is_unlimited(monkeypatch, tmp_path):
    folder = tmp_path / "media" / "9" / "original"
    folder.mkdir(parents=True)
    for i in range(3):
        Image.new("RGB", (20, 20), (0, i * 20, 0)).save(folder / f"p{i}.jpg")

    with patch("app.service.resolve_mise_folder") as resolve:
        resolve.return_value = (folder.resolve(), {"gallery_id": 9}, str(folder))
        r = client.post(
            "/analyze-folder",
            data={"mise_gallery_id": 9},
            headers=AUTH,
        )

    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3