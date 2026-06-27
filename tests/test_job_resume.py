"""Queued folder jobs resume from an existing partial run on retry."""

import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-resume-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "true"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db  # noqa: E402
from app.jobs import process_job  # noqa: E402
from app.vision import AnalysisResult, Culling  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_ENABLED", True)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    db._SCHEMA_READY = False
    db.init()
    yield


def test_incremental_job_resumes_after_partial_run(tmp_path, monkeypatch):
    folder = tmp_path / "gallery"
    folder.mkdir()
    for i in range(3):
        Image.new("RGB", (20, 20), (i * 30, 0, 0)).save(folder / f"img{i}.jpg")

    job_id = db.create_job(str(folder), limit=0, source="resume-test", model="mock:test")
    run_id = db.create_run(source="resume-test", model="mock:test")
    db.update_job(job_id, run_id=run_id, result={"progress": {"done": 1, "total": 3}})
    db.save_photo_analysis(
        run_id,
        {
            "image_path": str(folder / "img0.jpg"),
            "shot_type": "other",
            "keywords": ["seed"],
            "culling": {"keeper_score": 0.5, "hero_potential": 0.4, "technical_quality": "good"},
            "alt_text": "",
            "description": "",
            "suggested_iptc": {},
        },
    )

    calls: list[str] = []

    def _track(path, **kwargs):
        calls.append(Path(path).name)
        return AnalysisResult(
            image_path=str(path),
            keywords=["mock"],
            culling=Culling(keeper_score=0.8, hero_potential=0.7, technical_quality="good"),
        )

    monkeypatch.setattr("app.service.vision.analyze_image", _track)
    job = db.get_job(job_id)
    process_job(job)

    assert calls == ["img1.jpg", "img2.jpg"]
    final = db.get_job(job_id)
    assert final["status"] == "done"
    assert len(db.get_photos_for_run(run_id)) == 3