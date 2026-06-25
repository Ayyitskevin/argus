"""Mise → Argus analyze → export contract → writeback-shaped matching (mock vision)."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-mise-loop-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, mise_dedup, service  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.folder_fingerprint import folder_fingerprint  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer mise-loop-token"}

_WRITE_FIELDS = frozenset({"basename", "image_path", "keywords", "alt_text", "culling"})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "mise-loop-token")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "MISE_URL", "")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "")
    db._SCHEMA_READY = False
    db.init()
    set_auth_context(None)
    yield
    set_auth_context(None)


def _mise_gallery(tmp_path: Path, gallery_id: int = 1) -> Path:
    originals = tmp_path / "mise-media" / str(gallery_id) / "original"
    originals.mkdir(parents=True)
    Image.new("RGB", (80, 60), (200, 120, 40)).save(originals / "stored-hero.jpg")
    Image.new("RGB", (80, 60), (40, 120, 200)).save(originals / "stored-detail.jpg")
    return originals


def _basename_key(name: str) -> str:
    return Path(name).name.lower()


def _simulate_mise_writeback(export: dict, assets: list[dict]) -> dict:
    """Mirror mise-work argus_writeback.apply_to_gallery matching (no HTTP)."""
    by_stored = {_basename_key(a["stored"]): a for a in assets}
    by_filename = {_basename_key(a["filename"]): a for a in assets}
    matched: list[dict] = []
    hero_rows: list[tuple[float, str]] = []

    for photo in export.get("photos") or []:
        basename = _basename_key(str(photo.get("basename") or photo.get("image_path") or ""))
        if not basename:
            continue
        asset = by_stored.get(basename) or by_filename.get(basename)
        if not asset:
            continue
        culling = photo.get("culling") or {}
        hero = culling.get("hero_potential")
        matched.append(
            {
                "stored": asset["stored"],
                "keeper": culling.get("keeper_score"),
                "hero": hero,
                "keywords": photo.get("keywords") or [],
                "alt_text": photo.get("alt_text"),
            }
        )
        if hero is not None and float(hero) >= 0.5:
            hero_rows.append((float(hero), asset["stored"]))

    hero_rows.sort(key=lambda row: (-row[0], row[1]))
    return {
        "matched": len(matched),
        "photo_count": len(export.get("photos") or []),
        "hero_assets": [name for _, name in hero_rows[:5]],
        "rows": matched,
    }


def test_export_contract_and_writeback_matching(monkeypatch, tmp_path):
    gallery = _mise_gallery(tmp_path)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")

    result = service.perform_folder_analyze(
        mise_gallery_id=1,
        client_id="mise",
        limit=0,
    )
    run_id = int(result["run_id"])
    assert result["count"] == 2

    resp = client.get(f"/runs/{run_id}/export", headers=AUTH)
    assert resp.status_code == 200
    export = resp.json()
    assert export.get("photos")
    for photo in export["photos"]:
        assert _WRITE_FIELDS.issubset(photo.keys())
        assert photo.get("culling") is not None
        assert "keeper_score" in photo["culling"]

    assets = [
        {"stored": "stored-hero.jpg", "filename": "hero.jpg"},
        {"stored": "stored-detail.jpg", "filename": "detail.jpg"},
    ]
    wb = _simulate_mise_writeback(export, assets)
    assert wb["matched"] == 2
    assert wb["photo_count"] == 2
    assert all(row["stored"] in {"stored-hero.jpg", "stored-detail.jpg"} for row in wb["rows"])
    assert all(isinstance(row["keywords"], list) for row in wb["rows"])


def test_fingerprint_change_reanalyzes_after_done(monkeypatch, tmp_path):
    from unittest.mock import patch

    gallery = _mise_gallery(tmp_path)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)

    fp = folder_fingerprint(gallery)
    mise_dedup.record_done(1, "mise", 10, folder_fingerprint=fp)

    Image.new("RGB", (30, 30), (1, 2, 3)).save(gallery / "new-upload.jpg")

    with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
        with patch("app.db.create_job", return_value="job-after-upload") as create_job:
            out = service.perform_folder_analyze(mise_gallery_id=1, client_id="mise")

    assert create_job.called
    assert out.get("deduped") is not True
    assert out["job_id"] == "job-after-upload"


def test_skip_dedup_form_field_for_republish(monkeypatch, tmp_path):
    from unittest.mock import patch

    gallery = _mise_gallery(tmp_path)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)

    fp = folder_fingerprint(gallery)
    mise_dedup.record_queued(1, "mise", "job-stale", folder_fingerprint=fp)

    with patch("app.service.queue_accepting_jobs", return_value=(True, None)):
        with patch("app.db.create_job", return_value="job-republish") as create_job:
            resp = client.post(
                "/analyze-folder",
                data={
                    "mise_gallery_id": "1",
                    "skip_dedup": "true",
                    "limit": "0",
                },
                headers=AUTH,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert create_job.called
    assert body.get("deduped") is not True
    assert body.get("job_id") == "job-republish"