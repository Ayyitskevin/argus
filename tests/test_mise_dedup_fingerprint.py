"""Mise analyze dedup invalidates when folder contents change."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-dedup-fp-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, mise_dedup, service  # noqa: E402
from app.folder_fingerprint import folder_fingerprint  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "dedup-fp-token")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    db._SCHEMA_READY = False
    db.init()
    yield


def _gallery(tmp_path: Path, name: str = "a.jpg") -> Path:
    originals = tmp_path / "media" / "12" / "original"
    originals.mkdir(parents=True)
    Image.new("RGB", (40, 30), (10, 20, 30)).save(originals / name)
    return originals


def test_folder_fingerprint_changes_when_file_added(tmp_path):
    gallery = _gallery(tmp_path)
    fp1 = folder_fingerprint(gallery)
    Image.new("RGB", (20, 20), (1, 2, 3)).save(gallery / "b.jpg")
    fp2 = folder_fingerprint(gallery)
    assert fp1 != fp2


def test_lookup_ignores_stale_fingerprint(tmp_path):
    gallery = _gallery(tmp_path)
    fp_old = folder_fingerprint(gallery)
    mise_dedup.record_done(12, "client-x", 99, folder_fingerprint=fp_old)

    Image.new("RGB", (20, 20), (1, 2, 3)).save(gallery / "b.jpg")
    fp_new = folder_fingerprint(gallery)
    assert mise_dedup.lookup(12, "client-x", folder_fingerprint=fp_new) is None


def test_lookup_hits_when_fingerprint_matches(tmp_path):
    gallery = _gallery(tmp_path)
    fp = folder_fingerprint(gallery)
    mise_dedup.record_queued(12, "client-x", "job-fp", folder_fingerprint=fp)
    hit = mise_dedup.lookup(12, "client-x", folder_fingerprint=fp)
    assert hit is not None
    assert hit["job_id"] == "job-fp"


def test_perform_folder_analyze_requeues_after_folder_change(monkeypatch, tmp_path):
    gallery = _gallery(tmp_path)
    fp_old = folder_fingerprint(gallery)
    mise_dedup.record_done(12, "client-y", 50, folder_fingerprint=fp_old)

    Image.new("RGB", (20, 20), (1, 2, 3)).save(gallery / "b.jpg")

    with patch("app.service.resolve_mise_folder") as resolve:
        resolve.return_value = (gallery.resolve(), {"gallery_id": 12}, str(gallery))
        with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
            with patch("app.db.create_job", return_value="job-after-change") as create_job:
                out = service.perform_folder_analyze(mise_gallery_id=12, client_id="client-y")

    assert create_job.called
    assert out["job_id"] == "job-after-change"
    assert out.get("deduped") is not True