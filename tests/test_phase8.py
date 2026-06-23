"""Phase 8 tests — manifest, recursive analyze, POST /jobs, callbacks (mock only)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-phase8-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, service  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer phase8-test-token"}


@pytest.fixture(autouse=True)
def phase8_env(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "phase8-test-token")
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    from app import db  # noqa: WPS433

    db._SCHEMA_READY = False
    db.init()


@pytest.fixture
def nested_gallery(tmp_path) -> str:
    root = tmp_path / "gallery"
    nested = root / "selects"
    nested.mkdir(parents=True)
    Image.new("RGB", (800, 600), color=(40, 40, 40)).save(root / "root.jpg", format="JPEG")
    Image.new("RGB", (800, 600), color=(80, 80, 80)).save(nested / "inner.jpg", format="JPEG")
    return str(root)


def test_recursive_collects_nested_images(nested_gallery):
    shallow = service.analyze_folder_run(
        folder=Path(nested_gallery),
        source="test-shallow",
        limit=10,
        recursive=False,
    )
    deep = service.analyze_folder_run(
        folder=Path(nested_gallery),
        source="test-deep",
        limit=10,
        recursive=True,
    )
    assert shallow["count"] == 1
    assert deep["count"] == 2


def test_manifest_json_shape(nested_gallery):
    result = service.analyze_folder_run(
        folder=Path(nested_gallery),
        source="client:manifest-client|test",
        limit=5,
        recursive=True,
    )
    manifest = service.build_run_manifest(result["run_id"])
    assert manifest is not None
    assert manifest["run_id"] == result["run_id"]
    assert manifest["client_id"] == "manifest-client"
    assert manifest["photo_count"] == 2
    assert manifest["photos"][0]["sidecars"]["argus"].endswith(".argus.json")

    api = client.get(f"/runs/{result['run_id']}/manifest.json", headers=AUTH)
    assert api.status_code == 200
    assert api.json()["photo_count"] == 2


def test_post_jobs_runs_sync_when_queue_disabled(nested_gallery):
    resp = client.post(
        "/jobs",
        json={"folder": nested_gallery, "limit": 3, "recursive": True},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["count"] == 2


def test_callback_url_validation():
    assert service.validate_job_create(folder="/tmp", callback_url="http://evil.example/cb")[1]
    ok, err = service.validate_job_create(
        folder="/",
        callback_url="http://127.0.0.1:9000/hook",
    )
    assert err is None or ok is not None


def test_job_callback_fires_on_completion(nested_gallery):
    os.environ["ARGUS_QUEUE_ENABLED"] = "true"
    from app import db  # noqa: E402
    from app.jobs import process_job  # noqa: E402

    job_id = db.create_job(
        nested_gallery,
        limit=2,
        source="callback-test",
        callback_url="http://127.0.0.1:9999/done",
        recursive=True,
    )
    job = db.get_job(job_id)
    with patch("app.callbacks.httpx.post") as mock_post:
        mock_post.return_value.raise_for_status = lambda: None
        process_job(job)
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["status"] == "done"
        assert payload["result"]["count"] == 2
    os.environ["ARGUS_QUEUE_ENABLED"] = "false"