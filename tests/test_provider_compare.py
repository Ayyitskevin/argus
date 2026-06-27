"""Grok↔Qwen parity harness — pure comparison + endpoint. Mock/synthesized only."""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("ARGUS_VISION_BACKEND", "mock")
_TMP = tempfile.mkdtemp(prefix="argus-compare-")
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_QUEUE_ENABLED"] = "false"

from app import config, db, provider_compare, service  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", None)  # homelab-open for the endpoint
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "VISION_PREFILTER_ENABLED", False)
    db._SCHEMA_READY = False
    db.init()
    set_auth_context(None)
    yield
    set_auth_context(None)


def _photo(basename, keeper, hero, keywords, *, shot_type="hero_plate", alt="A plated dish.", cost=0.0, latency=100.0):
    return {
        "image_path": f"/gallery/{basename}",
        "basename": basename,
        "shot_type": shot_type,
        "keywords": keywords,
        "alt_text": alt,
        "culling": {"keeper_score": keeper, "hero_potential": hero},
        "cost_usd": cost,
        "latency_ms": latency,
    }


def _run(run_id, model, photos):
    return {"run": {"id": run_id, "model": model}, "photos": photos}


# --- provider label -----------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("grok:grok-4-fast", "grok"),
        ("grok-4-fast", "grok"),
        ("qwen:qwen3-vl:32b", "qwen"),
        ("qwen3-vl:32b", "qwen"),
        ("mock:mock", "mock"),
        ("", "unknown"),
    ],
)
def test_provider_of(model, expected):
    assert provider_compare.provider_of({"model": model}) == expected


def test_provider_of_prefers_explicit():
    assert provider_compare.provider_of({"provider": "qwen", "model": "grok-4-fast"}) == "qwen"


# --- comparison logic ---------------------------------------------------------

def test_identical_runs_within_tolerance():
    photos = [
        _photo("a.jpg", 0.8, 0.7, ["rim light", "steam"]),
        _photo("b.jpg", 0.4, 0.3, ["flatlay"]),
    ]
    a = _run(1, "grok:grok-4-fast", [dict(p) for p in photos])
    b = _run(2, "qwen:qwen3-vl:32b", [dict(p) for p in photos])
    rep = provider_compare.compare_provider_runs(a, b)
    assert rep["providers"] == {"a": "grok", "b": "qwen"}
    assert rep["photo_counts"]["common"] == 2
    assert rep["agreement"]["mean_keeper_abs_delta"] == 0.0
    assert rep["agreement"]["keyword_jaccard_mean"] == 1.0
    assert rep["agreement"]["shot_type_agree_rate"] == 1.0
    assert rep["verdict"]["within_tolerance"] is True
    assert rep["verdict"]["reasons"] == []


def test_divergent_runs_flagged():
    a = _run(1, "grok-4-fast", [_photo("a.jpg", 0.9, 0.9, ["rim light", "steam"], cost=0.002)])
    b = _run(2, "qwen3-vl:32b", [_photo("a.jpg", 0.2, 0.1, ["blurry"], shot_type="other", cost=0.0)])
    rep = provider_compare.compare_provider_runs(a, b)
    assert rep["agreement"]["mean_keeper_abs_delta"] == pytest.approx(0.7)
    assert rep["agreement"]["mean_hero_abs_delta"] == pytest.approx(0.8)
    assert rep["agreement"]["keyword_jaccard_mean"] == 0.0
    assert rep["agreement"]["shot_type_agree_rate"] == 0.0
    assert rep["verdict"]["within_tolerance"] is False
    assert any("keeper" in r for r in rep["verdict"]["reasons"])
    # cost: grok real, qwen 0 -> negative delta
    assert rep["cost_usd"]["a"] == pytest.approx(0.002)
    assert rep["cost_usd"]["b"] == 0.0
    assert rep["cost_usd"]["delta"] == pytest.approx(-0.002)


def test_only_in_each_and_keyword_jaccard():
    a = _run(1, "grok", [_photo("a.jpg", 0.8, 0.7, ["x", "y"]), _photo("solo_a.jpg", 0.5, 0.5, ["z"])])
    b = _run(2, "qwen", [_photo("a.jpg", 0.8, 0.7, ["x", "w"]), _photo("solo_b.jpg", 0.5, 0.5, ["z"])])
    rep = provider_compare.compare_provider_runs(a, b)
    assert rep["photo_counts"]["common"] == 1
    assert rep["photo_counts"]["only_a"] == 1
    assert rep["photo_counts"]["only_b"] == 1
    assert rep["only_in_a"] == ["solo_a.jpg"]
    assert rep["only_in_b"] == ["solo_b.jpg"]
    # jaccard of {x,y} vs {x,w} = 1/3
    assert rep["per_photo"][0]["keyword_jaccard"] == pytest.approx(1 / 3, abs=1e-3)


def test_no_overlap_not_within_tolerance():
    a = _run(1, "grok", [_photo("a.jpg", 0.8, 0.7, ["x"])])
    b = _run(2, "qwen", [_photo("b.jpg", 0.8, 0.7, ["x"])])
    rep = provider_compare.compare_provider_runs(a, b)
    assert rep["photo_counts"]["common"] == 0
    assert rep["verdict"]["within_tolerance"] is False
    assert any("overlapping" in r for r in rep["verdict"]["reasons"])


def test_per_photo_sorted_by_worst_keeper():
    a = _run(1, "grok", [_photo("small.jpg", 0.50, 0.5, ["x"]), _photo("big.jpg", 0.90, 0.5, ["x"])])
    b = _run(2, "qwen", [_photo("small.jpg", 0.55, 0.5, ["x"]), _photo("big.jpg", 0.20, 0.5, ["x"])])
    rep = provider_compare.compare_provider_runs(a, b)
    assert rep["per_photo"][0]["basename"] == "big.jpg"  # 0.70 delta first
    assert rep["per_photo"][1]["basename"] == "small.jpg"


# --- endpoint -----------------------------------------------------------------

def _mise_folder(tmp_path):
    g = tmp_path / "gallery"
    g.mkdir()
    Image.new("RGB", (80, 60), (200, 120, 40)).save(g / "hero.jpg")
    Image.new("RGB", (60, 80), (40, 120, 200)).save(g / "detail.jpg")
    return g


def test_compare_providers_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_MEDIA_ROOTS", [tmp_path])
    folder = _mise_folder(tmp_path)
    run_a = int(service.perform_folder_analyze(folder=str(folder), limit=0)["run_id"])
    run_b = int(service.perform_folder_analyze(folder=str(folder), limit=0)["run_id"])

    resp = client.get("/runs/compare/providers", params={"a": run_a, "b": run_b})
    assert resp.status_code == 200
    rep = resp.json()
    assert rep["runs"] == {"a": run_a, "b": run_b}
    assert rep["photo_counts"]["common"] == 2
    # deterministic mock -> identical scores -> within tolerance
    assert rep["verdict"]["within_tolerance"] is True
    assert rep["cost_usd"]["a"] >= 0


def test_compare_providers_endpoint_missing_run():
    resp = client.get("/runs/compare/providers", params={"a": 999991, "b": 999992})
    assert resp.status_code == 404
