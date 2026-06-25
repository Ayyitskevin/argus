"""Phase 7 tests — review UI helpers, PATCH corrections, run compare (mock only)."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-phase7-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, service  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer phase7-test-token"}


@pytest.fixture(autouse=True)
def enable_auth(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "phase7-test-token")


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (1200, 800), color=(100, 80, 60)).save(path, format="JPEG")
    return str(path)


def _analyze_with_client(sample_image: str, client_id: str = "cull-client") -> int:
    r = client.post(
        "/analyze",
        data={"path": sample_image, "client_id": client_id},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_extract_client_id_from_source():
    assert service.extract_client_id("client:platekit|/tmp/gallery") == "platekit"
    assert service.extract_client_id("/plain/path") is None


def test_sort_and_filter_photos_hides_low_keepers():
    photos = [
        {"basename": "weak.jpg", "culling": {"keeper_score": 0.1}, "keywords": []},
        {"basename": "strong.jpg", "culling": {"keeper_score": 0.9}, "keywords": []},
    ]
    filtered = service.sort_and_filter_photos(photos, min_keeper=0.3)
    assert len(filtered) == 1
    assert filtered[0]["basename"] == "strong.jpg"


def test_sort_and_filter_photos_by_keeper():
    photos = [
        {"basename": "b.jpg", "culling": {"keeper_score": 0.4}, "keywords": ["food"]},
        {"basename": "a.jpg", "culling": {"keeper_score": 0.9}, "keywords": ["chef"]},
    ]
    sorted_photos = service.sort_and_filter_photos(photos, sort="keeper")
    assert sorted_photos[0]["basename"] == "a.jpg"
    filtered = service.sort_and_filter_photos(photos, keyword="chef")
    assert len(filtered) == 1


def test_patch_photo_updates_db_and_prefs(sample_image):
    run_id = _analyze_with_client(sample_image)
    photo_id = db.get_photos_for_run(run_id)[0]["id"]

    denied = client.patch(
        f"/runs/{run_id}/photo/{photo_id}",
        json={"keywords": ["hand-edited", "plated"]},
    )
    assert denied.status_code == 401

    ok = client.patch(
        f"/runs/{run_id}/photo/{photo_id}",
        json={"keywords": ["hand-edited", "plated"], "keeper_score": 0.95},
        headers=AUTH,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True
    assert body["prefs_updated"] is True
    assert body["photo"]["keywords"] == ["hand-edited", "plated"]
    assert body["photo"]["culling"]["keeper_score"] == 0.95

    prefs = db.get_preferences("cull-client")
    assert "hand-edited" in prefs.get("keyword_boosts", [])


def test_promote_keywords_without_replacing_keywords(sample_image):
    run_id = _analyze_with_client(sample_image, client_id="boost-client")
    photo_id = db.get_photos_for_run(run_id)[0]["id"]
    before = db.get_photo_for_run(run_id, photo_id)

    ok = client.patch(
        f"/runs/{run_id}/photo/{photo_id}",
        json={"promote_keywords": ["signature-dish"]},
        headers=AUTH,
    )
    assert ok.status_code == 200
    after = db.get_photo_for_run(run_id, photo_id)
    assert after["keywords"] == before["keywords"]
    assert "signature-dish" in db.get_preferences("boost-client").get("keyword_boosts", [])


def test_compare_runs_reports_score_drift(sample_image):
    run_a = _analyze_with_client(sample_image, client_id="compare-a")
    run_b = _analyze_with_client(sample_image, client_id="compare-b")
    body = client.get(f"/runs/compare?a={run_a}&b={run_b}").json()
    assert body["common_paths"] == 1
    assert body["a"]["run_id"] == run_a
    assert body["b"]["run_id"] == run_b
    assert body["score_changes"][0]["keeper_delta"] == 0.0


def test_run_page_renders_culling_ui(sample_image):
    run_id = _analyze_with_client(sample_image)
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    html = page.text
    assert "Culling grid" in html
    assert "Hero candidates" in html
    assert "/thumb/" in html


def test_photos_grid_partial_supports_filters(sample_image):
    run_id = _analyze_with_client(sample_image)
    grid = client.get(f"/runs/{run_id}/photos-grid?sort=keeper&min_keeper=0")
    assert grid.status_code == 200
    assert "photo-" in grid.text


def test_patch_photo_accepts_ui_cookie(sample_image):
    from app.auth import UI_TOKEN_COOKIE

    run_id = _analyze_with_client(sample_image)
    photo_id = db.get_photos_for_run(run_id)[0]["id"]
    client.cookies.set(UI_TOKEN_COOKIE, "phase7-test-token")
    ok = client.patch(
        f"/runs/{run_id}/photo/{photo_id}",
        json={"keeper_score": 0.88},
    )
    assert ok.status_code == 200, ok.text