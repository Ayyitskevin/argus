"""Phase 4 tests — history prefs, auth, metrics (mock backend only)."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-phase4-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, service, vision  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer phase4-test-token"}


@pytest.fixture(autouse=True)
def enable_auth(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "phase4-test-token")


@pytest.fixture(scope="module")
def sample_image() -> str:
    path = Path(_TMP) / "sample.jpg"
    Image.new("RGB", (1200, 800), color=(90, 120, 60)).save(path, format="JPEG")
    return str(path)


def test_healthz_reports_auth_enabled():
    body = client.get("/healthz").json()
    assert body["auth_enabled"] is True


def test_analyze_requires_bearer(sample_image):
    denied = client.post("/analyze", data={"path": sample_image})
    assert denied.status_code == 401

    ok = client.post("/analyze", data={"path": sample_image}, headers=AUTH)
    assert ok.status_code == 200


def test_metrics_increment_on_analyze(sample_image):
    before = client.get("/metrics").json()["counters"]["analyze_single"]
    client.post("/analyze", data={"path": sample_image}, headers=AUTH)
    after = client.get("/metrics").json()["counters"]["analyze_single"]
    assert after == before + 1


def test_history_stats_aggregate_keywords_and_shot_types(sample_image):
    folder = str(Path(sample_image).parent)
    for idx in range(3):
        client.post(
            "/analyze",
            data={"path": sample_image, "client_id": "platekit"},
            headers=AUTH,
        )
    client.post(
        "/analyze-folder",
        data={"folder": folder, "limit": 2, "client_id": "platekit"},
        headers=AUTH,
    )

    stats = db.get_client_history_stats("platekit")
    assert stats["num_runs"] >= 4
    assert stats["num_photos"] >= 4
    assert stats["top_shot_type"] == "hero_plate"
    assert stats["top_keywords"]
    assert stats["avg_keeper_score"] is not None


def test_load_preferences_merges_history_when_explicit_prefs_missing(sample_image):
    client.post(
        "/analyze",
        data={"path": sample_image, "client_id": "history-merge"},
        headers=AUTH,
    )
    prefs = service.load_preferences("history-merge")
    assert prefs.get("shot_type_preference") == "hero_plate"
    assert prefs.get("keyword_boosts")
    assert prefs.get("culling_bias", 0) > 0


def test_explicit_prefs_override_history_keyword_boosts():
    db.set_preferences("override-client", {"keyword_boosts": ["custom-tag"]})
    db.create_run(source="client:override-client|/tmp", model="mock:test")
    prefs = service.load_preferences("override-client")
    assert prefs["keyword_boosts"] == ["custom-tag"]


def test_shot_type_preference_boosts_matching_results():
    boosted = vision._apply_prefs(
        vision._mock_result("/tmp/hero.jpg", 1200, 800, "mock:test"),
        {"shot_type_preference": "hero_plate"},
    )
    plain = vision._apply_prefs(
        vision._mock_result("/tmp/hero.jpg", 1200, 800, "mock:test"),
        {},
    )
    assert boosted.culling.hero_potential > plain.culling.hero_potential


def test_client_history_endpoint(sample_image):
    client.post(
        "/analyze",
        data={"path": sample_image, "client_id": "endpoint-client"},
        headers=AUTH,
    )
    body = client.get("/clients/endpoint-client/history", headers=AUTH).json()
    assert body["client_id"] == "endpoint-client"
    assert body["num_photos"] >= 1


def test_preferences_post_requires_auth():
    denied = client.post(
        "/preferences",
        data={"client_id": "x", "prefs": json.dumps({"keyword_boosts": ["a"]})},
    )
    assert denied.status_code == 401