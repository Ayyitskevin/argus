"""Phase 0 smoke test — proves argus actually boots and serves on the mock backend.

Runs fully offline: ARGUS_VISION_BACKEND=mock means no Ollama call, and the
queue is disabled so /analyze-folder runs synchronously and deterministically.
A temp JPEG is generated with Pillow because the repo ships no sample images.
"""

import os
import tempfile
from pathlib import Path

# Config is read at import time, so the environment must be set before app/db
# are imported. Isolate the DB and data dir into a temp dir per run.
_TMP = tempfile.mkdtemp(prefix="argus-smoke-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app

client = TestClient(app)


@pytest.fixture(scope="module")
def sample_image() -> str:
    """A real (if boring) landscape JPEG on disk for the local-path code paths."""
    p = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (1200, 800), color=(120, 90, 60)).save(p, format="JPEG")
    return str(p)


def test_healthz_reports_mock_backend():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["backend"] == "mock"
    assert body["queue_enabled"] is False
    assert body["auth_enabled"] is False


def test_analyze_single_local_path(sample_image):
    r = client.post("/analyze", data={"path": sample_image})
    assert r.status_code == 200, r.text
    body = r.json()
    # Mock backend marks the model so we never mistake it for a real run.
    assert body["model"].startswith("mock:")
    assert body["shot_type"] == "hero_plate"  # landscape -> hero_plate
    assert 0.0 <= body["culling"]["keeper_score"] <= 1.0
    assert "mock" in body["keywords"]
    assert "run_id" in body and body["run_url"].startswith("/runs/")


def test_analyze_single_missing_path():
    r = client.post("/analyze", data={"path": "/no/such/file.jpg"})
    assert r.status_code == 404


def test_analyze_folder_sync(sample_image):
    folder = str(Path(sample_image).parent)
    r = client.post("/analyze-folder", data={"folder": folder, "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    # Queue disabled -> synchronous run with photos inline, not a job_id.
    assert "job_id" not in body
    assert body["count"] >= 1
    assert body["photos"][0]["model"].startswith("mock:")
    run_id = body["run_id"]

    # The run is persisted and exportable.
    exp = client.get(f"/runs/{run_id}/export")
    assert exp.status_code == 200
    assert exp.json()["run"]["id"] == run_id


def test_write_sidecars_creates_json_iptc_and_xmp(sample_image, tmp_path):
    from app.sidecars import write_sidecar

    analysis = {
        "image_path": sample_image,
        "keywords": ["chef plated", "warm light"],
        "suggested_iptc": {
            "headline": "Plated Dish",
            "caption": "A plated dish in warm light.",
            "keywords": ["restaurant", "plated dish"],
        },
    }
    written = write_sidecar(sample_image, analysis, sidecar_dir=tmp_path)
    assert set(written) == {"argus", "iptc", "xmp"}
    assert written["argus"].exists()
    assert written["iptc"].exists()
    assert written["xmp"].read_text(encoding="utf-8").startswith("<?xpacket")


def test_job_claim_is_atomic(sample_image):
    from app import db

    folder = str(Path(sample_image).parent)
    job_id = db.create_job(folder, source="test-source", model="mock:test")
    claimed = db.claim_next_job()
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    assert db.claim_next_job() is None


def test_analyze_upload_does_not_write_upload_sidecar(sample_image):
    with open(sample_image, "rb") as f:
        r = client.post(
            "/analyze",
            data={"write_sidecar": "true"},
            files={"file": ("../escape.jpg", f, "image/jpeg")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"].startswith("mock:")
    assert body["sidecar_warning"].startswith("sidecar not written")


def test_client_uses_shared_xmp_builder(sample_image):
    from app.client import ArgusClient

    photo = {
        "image_path": sample_image,
        "keywords": ["shared builder"],
        "suggested_iptc": {"headline": "Shared", "caption": "Shared caption."},
    }
    xmp = ArgusClient()._build_xmp(photo)
    assert "Shared" in xmp
    assert "Shared caption." in xmp
