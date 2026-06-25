"""Homelab pipeline dashboard helpers."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="argus-pipeline-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, pipeline  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer pipeline-test"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "API_TOKEN", "pipeline-test")
    monkeypatch.setattr(config, "MISE_URL", "")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "")
    monkeypatch.setattr(config, "PLUTUS_URL", "")
    monkeypatch.setattr(config, "PLUTUS_PUBLIC_URL", "")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "")
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    db._SCHEMA_READY = False
    db.init()
    yield


def test_pipeline_snapshot_empty_without_mise():
    snap = pipeline.pipeline_snapshot()
    assert snap["counts"]["published"] == 0
    assert snap["handoff"]["mise_configured"] is False


def test_gallery_rows_from_mise(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "secret")
    payload = {
        "galleries": [
            {
                "id": 3,
                "title": "Demo",
                "published": True,
                "argus_last_run_id": 9,
                "argus_last_status": "done",
                "plutus_last_run_id": 2,
                "plutus_last_status": "done",
            }
        ]
    }
    with patch("app.mise_client.httpx.Client") as mock_client:
        inst = mock_client.return_value.__enter__.return_value
        inst.get.return_value.status_code = 200
        inst.get.return_value.json.return_value = payload
        rows = pipeline.gallery_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == 3
    assert rows[0]["argus_run_id"] == 9
    assert rows[0]["plutus_run_id"] == 2


def test_ui_pipeline_requires_homelab(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", True)
    r = client.get("/ui/pipeline", follow_redirects=False, headers=AUTH)
    assert r.status_code == 303


def test_run_all_uses_studio_links_from_recommend(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "secret")
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "hook")
    payload = {
        "galleries": [
            {
                "id": 1,
                "title": "Tasting",
                "published": True,
                "argus_last_run_id": 9,
                "argus_last_status": "done",
                "plutus_last_run_id": None,
                "plutus_last_status": None,
            }
        ]
    }
    with patch("app.mise_client.httpx.Client") as mock_client:
        inst = mock_client.return_value.__enter__.return_value
        inst.get.return_value.status_code = 200
        inst.get.return_value.json.return_value = payload
        with patch(
            "app.plutus_client.recommend_mise_gallery",
            return_value={
                "run_id": 42,
                "bundles": [{}],
                "bundle_count": 1,
                "estimated_total_cents": 9900,
                "review_url": "http://plutus:8030/runs/42",
                "pitch_url": "http://plutus:8030/runs/42/pitch.txt",
            },
        ):
            with patch("app.mise_client.plutus_callback") as cb:
                with patch("app.plutus_client.create_share_link") as share:
                    result = pipeline.run_all(1)
    share.assert_not_called()
    assert result["review_url"] == "http://plutus:8030/runs/42"
    assert result["pitch_url"] == "http://plutus:8030/runs/42/pitch.txt"
    assert result["plutus_run_id"] == 42
    cb.assert_called_once()
    assert cb.call_args.kwargs["review_url"] == "http://plutus:8030/runs/42"


def test_run_all_skips_completed_steps(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "secret")
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "secret")
    payload = {
        "galleries": [
            {
                "id": 1,
                "title": "Tasting",
                "published": True,
                "argus_last_run_id": 9,
                "argus_last_status": "done",
                "plutus_last_run_id": 6,
                "plutus_last_status": "done",
            }
        ]
    }
    with patch("app.mise_client.httpx.Client") as mock_client:
        inst = mock_client.return_value.__enter__.return_value
        inst.get.return_value.status_code = 200
        inst.get.return_value.json.return_value = payload
        with patch("app.mise_client.plutus_callback"):
            with patch("app.plutus_client.create_share_link") as share:
                result = pipeline.run_all(1)
    share.assert_not_called()
    assert result["argus_run_id"] == 9
    assert result["plutus_run_id"] == 6
    assert result["review_url"] == "http://plutus:8030/runs/6"
    assert result["pitch_url"] == "http://plutus:8030/runs/6/pitch.txt"
    assert any("skipped" in s for s in result["steps"])


def test_ui_run_all_redirects_with_studio_links(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "secret")
    with patch(
        "app.pipeline.run_all",
        return_value={
            "steps": ["vision skipped (run 9)", "bundles skipped (run 6)", "review + pitch ready"],
            "review_url": "http://plutus:8030/runs/6",
            "pitch_url": "http://plutus:8030/runs/6/pitch.txt",
        },
    ):
        r = client.post(
            "/ui/pipeline/run-all/1",
            data={"api_token": "pipeline-test"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "review_url=" in loc
    assert "pitch_url=" in loc
    assert "offer_url=" not in loc


def test_ui_pipeline_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MISE_URL", "http://flow:8400")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "secret")
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "secret")
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path)
    media = tmp_path / "1" / "original"
    media.mkdir(parents=True)
    (media / "a.jpg").write_bytes(b"jpeg")
    payload = {
        "galleries": [
            {
                "id": 1,
                "title": "Demo",
                "published": True,
                "argus_last_run_id": None,
                "argus_last_status": None,
            }
        ]
    }
    with patch("app.mise_client.httpx.Client") as mock_client:
        inst = mock_client.return_value.__enter__.return_value
        inst.get.return_value.status_code = 200
        inst.get.return_value.json.return_value = payload
        with patch("app.plutus_client.connectivity", return_value={"configured": True, "reachable": True}):
            r = client.get("/ui/pipeline")
    assert r.status_code == 200
    assert "Homelab pipeline" in r.text
    assert "Run all" in r.text
    assert "Offer" not in r.text