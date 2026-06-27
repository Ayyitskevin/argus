"""Structured-output mode (Mise vision cutover) — schema conformance, cost
reporting, idempotency, and flag gating. Mock backend only; no network."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-structured-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, service, structured_output  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer structured-token"}

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "vision.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)


def _assert_valid(payload: dict) -> None:
    errors = sorted(_VALIDATOR.iter_errors(payload), key=lambda e: e.path)
    assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "structured-token")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "MISE_URL", "")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "")
    monkeypatch.setattr(config, "STRUCTURED_OUTPUT_ENABLED", True)
    # Run the real mock-analysis path (solid-color test tiles would otherwise be
    # prefiltered as non-photographic and cost $0), so cost reporting is exercised.
    monkeypatch.setattr(config, "VISION_PREFILTER_ENABLED", False)
    db._SCHEMA_READY = False
    db.init()
    set_auth_context(None)
    yield
    set_auth_context(None)


def _mise_gallery(tmp_path: Path, gallery_id: int = 1) -> Path:
    originals = tmp_path / "mise-media" / str(gallery_id) / "original"
    originals.mkdir(parents=True)
    Image.new("RGB", (80, 60), (200, 120, 40)).save(originals / "stored-hero.jpg")
    Image.new("RGB", (60, 80), (40, 120, 200)).save(originals / "stored-detail.jpg")
    return originals


# --- pure serializer ---------------------------------------------------------

def test_photo_to_vision_flattens_culling():
    photo = {
        "image_path": "/data/x/stored-hero.jpg",
        "keywords": ["rim light", " ", "steam"],
        "alt_text": "  Plated\nscallop  ",
        "culling": {"keeper_score": 0.82, "hero_potential": 0.61},
    }
    out = structured_output.photo_to_vision(photo)
    assert out == {
        "basename": "stored-hero.jpg",
        "keywords": ["rim light", "steam"],
        "alt_text": "Plated scallop",
        "keeper_score": 0.82,
        "hero_potential": 0.61,
    }


def test_scores_clamped_into_range():
    photo = {
        "basename": "a.jpg",
        "culling": {"keeper_score": 1.7, "hero_potential": -0.4},
    }
    out = structured_output.photo_to_vision(photo)
    assert out["keeper_score"] == 1.0
    assert out["hero_potential"] == 0.0


def test_missing_scores_and_alt_become_null():
    out = structured_output.photo_to_vision({"basename": "a.jpg", "culling": {}})
    assert out["keeper_score"] is None
    assert out["hero_potential"] is None
    assert out["alt_text"] is None
    assert out["keywords"] == []


def test_basename_from_basename_field_preferred():
    out = structured_output.photo_to_vision(
        {"basename": "real.jpg", "image_path": "/x/other.jpg", "culling": {}}
    )
    assert out["basename"] == "real.jpg"


def test_photos_without_basename_dropped():
    photos = [{"culling": {}}, {"basename": "keep.jpg", "culling": {}}]
    out = structured_output.photos_to_vision(photos)
    assert [p["basename"] for p in out] == ["keep.jpg"]


def test_serializer_output_validates_against_schema():
    photos = [
        {"image_path": "/x/a.jpg", "keywords": ["k"], "alt_text": "alt",
         "culling": {"keeper_score": 0.5, "hero_potential": 0.9}},
        {"image_path": "/x/b.jpg", "keywords": [], "alt_text": None,
         "culling": {"keeper_score": 5.0}},  # out of range -> clamped to 1.0
    ]
    payload = structured_output.run_to_vision({"photos": photos})
    _assert_valid(payload)
    assert payload["photos"][1]["keeper_score"] == 1.0


# --- cost / latency aggregation ----------------------------------------------

def test_aggregate_handles_cost_usd_and_micros():
    cost, latency = structured_output.aggregate_cost_latency(
        [
            {"cost_usd": 0.01, "latency_ms": 100.0},
            {"cost_micro_usd": 20_000, "latency_ms": 50.0},  # = $0.02
            {"latency_ms": 25.0},  # no cost
        ]
    )
    assert cost == pytest.approx(0.03)
    assert latency == pytest.approx(175.0)


def test_build_callback_payload_shape():
    full_run = {
        "photos": [
            {"image_path": "/x/a.jpg", "keywords": ["k"], "alt_text": "alt",
             "culling": {"keeper_score": 0.5, "hero_potential": 0.9},
             "cost_usd": 0.012, "latency_ms": 120.0},
        ]
    }
    payload = structured_output.build_callback_payload(
        full_run, gallery_id=42, run_id=7, correlation_id="corr-1"
    )
    _assert_valid(payload)
    assert payload["gallery_id"] == 42
    assert payload["run_id"] == 7
    assert payload["correlation_id"] == "corr-1"
    assert payload["status"] == "done"
    assert payload["provider"] == config.STRUCTURED_PROVIDER
    assert payload["cost_usd"] == pytest.approx(0.012)
    assert payload["latency_ms"] == pytest.approx(120.0)


def test_callback_payload_omits_correlation_when_absent():
    payload = structured_output.build_callback_payload(
        {"photos": []}, gallery_id=1, run_id=1
    )
    assert "correlation_id" not in payload


# --- end-to-end through a real mock analyze ----------------------------------

def test_structured_export_endpoint_validates(monkeypatch, tmp_path):
    _mise_gallery(tmp_path)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")

    result = service.perform_folder_analyze(mise_gallery_id=1, client_id="mise", limit=0)
    run_id = int(result["run_id"])
    assert result["count"] == 2

    # plain schema shape
    resp = client.get(f"/runs/{run_id}/structured", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    _assert_valid(body)
    assert {p["basename"] for p in body["photos"]} == {"stored-hero.jpg", "stored-detail.jpg"}
    for photo in body["photos"]:
        assert set(photo.keys()) == {"basename", "keywords", "alt_text", "keeper_score", "hero_potential"}

    # full callback body shape (with gallery_id)
    resp = client.get(
        f"/runs/{run_id}/structured",
        params={"gallery_id": 1, "correlation_id": "abc"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    cb = resp.json()
    _assert_valid(cb)
    assert cb["gallery_id"] == 1
    assert cb["correlation_id"] == "abc"
    assert cb["cost_usd"] > 0  # mock backend records simulated per-image cost
    assert cb["latency_ms"] >= 0


def test_structured_export_idempotent(monkeypatch, tmp_path):
    _mise_gallery(tmp_path)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")
    result = service.perform_folder_analyze(mise_gallery_id=1, client_id="mise", limit=0)
    run_id = int(result["run_id"])

    first = client.get(f"/runs/{run_id}/structured", params={"gallery_id": 1}, headers=AUTH).json()
    second = client.get(f"/runs/{run_id}/structured", params={"gallery_id": 1}, headers=AUTH).json()
    assert first == second  # pure function of the persisted run


# --- callback wiring / flag gating -------------------------------------------

def test_callback_fired_when_enabled(monkeypatch, tmp_path):
    _mise_gallery(tmp_path, gallery_id=7)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")
    calls = []
    monkeypatch.setattr(
        "app.mise_client.argus_callback",
        lambda gallery_id, payload, **kw: calls.append((gallery_id, payload)),
    )
    service.perform_folder_analyze(mise_gallery_id=7, client_id="mise", limit=0,
                                   correlation_id="cid-9")
    assert len(calls) == 1
    gallery_id, payload = calls[0]
    assert gallery_id == 7
    assert payload["correlation_id"] == "cid-9"
    assert payload["cost_usd"] > 0
    _assert_valid(payload)


def test_callback_not_fired_when_disabled(monkeypatch, tmp_path):
    _mise_gallery(tmp_path, gallery_id=7)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "mise-media")
    monkeypatch.setattr(config, "STRUCTURED_OUTPUT_ENABLED", False)
    calls = []
    monkeypatch.setattr(
        "app.mise_client.argus_callback",
        lambda gallery_id, payload, **kw: calls.append((gallery_id, payload)),
    )
    service.perform_folder_analyze(mise_gallery_id=7, client_id="mise", limit=0)
    assert calls == []


def test_callback_not_fired_for_non_mise_run(monkeypatch, tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    Image.new("RGB", (40, 40), (1, 2, 3)).save(folder / "p.jpg")
    monkeypatch.setattr(config, "ALLOWED_MEDIA_ROOTS", [tmp_path])
    calls = []
    monkeypatch.setattr(
        "app.mise_client.argus_callback",
        lambda gallery_id, payload, **kw: calls.append((gallery_id, payload)),
    )
    service.perform_folder_analyze(folder=str(folder), limit=0)
    assert calls == []


def test_argus_callback_noops_when_mise_unconfigured(monkeypatch):
    from app import mise_client

    posted = []
    monkeypatch.setattr(mise_client, "_post_argus_callback", lambda *a, **k: posted.append(a))
    # MISE_URL/token empty (fixture) -> not enabled -> no post, no thread, no error
    mise_client.argus_callback(1, {"run_id": 1}, background=False)
    assert posted == []


def test_argus_callback_posts_when_configured(monkeypatch):
    from app import mise_client

    monkeypatch.setattr(config, "MISE_URL", "http://mise.local")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "tok")
    posted = []
    monkeypatch.setattr(mise_client, "_post_argus_callback", lambda *a, **k: posted.append(a))
    mise_client.argus_callback(5, {"run_id": 9}, background=False)
    assert posted == [(5, {"run_id": 9})]
