"""Phase 6 — Mise gallery index proxy and API path resolution (mock HTTP only)."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-phase6-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, mise_client, service  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer phase6-test-token"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "phase6-test-token")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "MISE_URL", "")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "")
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", None)
    db._SCHEMA_READY = False
    db.init()
    set_auth_context(None)
    yield
    set_auth_context(None)


def test_mise_client_disabled_by_default():
    assert mise_client.is_enabled() is False


def test_list_mise_galleries_503_when_unconfigured():
    r = client.get("/mise/galleries", headers=AUTH)
    assert r.status_code == 503


def test_list_mise_galleries_requires_bearer(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "mise-secret")
    assert client.get("/mise/galleries").status_code == 401


def test_list_mise_galleries_proxies_mise(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "mise-secret")
    payload = {"galleries": [{"id": 7, "slug": "demo", "title": "Demo", "published": True}]}

    with patch("app.mise_client.httpx.Client") as mock_client:
        inst = mock_client.return_value.__enter__.return_value
        inst.get.return_value.status_code = 200
        inst.get.return_value.json.return_value = payload
        r = client.get("/mise/galleries", headers=AUTH)

    assert r.status_code == 200
    assert r.json() == payload
    inst.get.assert_called_once()
    args, kwargs = inst.get.call_args
    assert args[0] == "http://flow:8400/api/galleries"
    assert kwargs["params"] == {"published": "true"}
    assert kwargs["headers"]["Authorization"] == "Bearer mise-secret"


def test_resolve_mise_folder_via_api_originals_path(monkeypatch, tmp_path):
    originals = tmp_path / "media" / "42" / "original"
    originals.mkdir(parents=True)
    Image.new("RGB", (40, 30), (10, 20, 30)).save(originals / "a.jpg")

    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "mise-secret")
    monkeypatch.setattr(
        mise_client,
        "get_gallery",
        lambda gid: {"id": gid, "originals_path": str(originals)} if gid == 42 else None,
    )

    path, info, attempted = service.resolve_mise_folder(mise_gallery_id=42)
    assert path == originals.resolve()
    assert info["gallery_id"] == 42
    assert attempted == str(originals)


def test_mise_dedup_returns_existing_job(monkeypatch, tmp_path):
    from app import mise_dedup

    originals = tmp_path / "media" / "5" / "original"
    originals.mkdir(parents=True)
    Image.new("RGB", (40, 30), (10, 20, 30)).save(originals / "a.jpg")
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    mise_dedup.record_queued(5, "client-a", "job-existing")

    with patch("app.service.resolve_mise_folder") as resolve:
        resolve.return_value = (originals.resolve(), {"gallery_id": 5}, str(originals))
        with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
            with patch("app.db.create_job") as create_job:
                create_job.side_effect = AssertionError("should not create a second job")
                out = service.perform_folder_analyze(mise_gallery_id=5, client_id="client-a")

    assert out["deduped"] is True
    assert out["job_id"] == "job-existing"


def test_mise_dedup_skipped_when_requested(monkeypatch, tmp_path):
    from app import mise_dedup

    originals = tmp_path / "media" / "6" / "original"
    originals.mkdir(parents=True)
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    mise_dedup.record_queued(6, None, "job-old")

    with patch("app.service.resolve_mise_folder") as resolve:
        resolve.return_value = (originals.resolve(), {"gallery_id": 6}, str(originals))
        with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
            with patch("app.db.create_job", return_value="job-new") as create_job:
                out = service.perform_folder_analyze(
                    mise_gallery_id=6, skip_dedup=True,
                )
    assert create_job.called
    assert out["job_id"] == "job-new"


def test_resolve_mise_folder_still_uses_media_root(monkeypatch, tmp_path):
    root = tmp_path / "mise-media"
    gallery = root / "9" / "original"
    gallery.mkdir(parents=True)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", root)

    path, info, _ = service.resolve_mise_folder(mise_gallery_id=9)
    assert path == gallery.resolve()
    assert info["gallery_id"] == 9