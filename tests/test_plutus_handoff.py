"""Homelab Plutus hand-off after Mise gallery analyze."""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest

_TMP = tempfile.mkdtemp(prefix="argus-plutus-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, plutus_client  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "")
    yield


def test_plutus_client_disabled_by_default():
    assert plutus_client.is_enabled() is False


def test_studio_links_for_run_from_result(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    links = plutus_client.studio_links_for_run(
        5,
        {
            "run_id": 5,
            "review_url": "http://plutus.test/runs/5",
            "pitch_url": "http://plutus.test/runs/5/pitch.txt",
        },
    )
    assert links["review_url"] == "http://plutus.test/runs/5"
    assert links["pitch_url"] == "http://plutus.test/runs/5/pitch.txt"


def test_studio_links_for_run_fallback(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://127.0.0.1:8030")
    monkeypatch.setattr(config, "PLUTUS_PUBLIC_URL", "http://plutus.test:8030")
    links = plutus_client.studio_links_for_run(7)
    assert links == {
        "review_url": "http://plutus.test:8030/runs/7",
        "pitch_url": "http://plutus.test:8030/runs/7/pitch.txt",
    }


def test_recommend_mise_gallery_posts(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "shared-secret")
    captured: dict = {}

    class _Resp:
        def read(self):
            return json.dumps({"run_id": 9, "bundles": [{"id": "canvas"}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = req.data.decode()
        return _Resp()

    with patch("app.plutus_client.urllib.request.urlopen", fake_urlopen):
        result = plutus_client.recommend_mise_gallery(3, argus_run_id=42)

    assert result["run_id"] == 9
    assert "mise_gallery_id=3" in captured["body"]
    assert "argus_run_id=42" in captured["body"]
    assert captured["auth"] == "Bearer shared-secret"
    assert captured["url"] == "http://plutus:8030/recommend/mise-gallery"


def test_handoff_async_noop_when_disabled():
    plutus_client.handoff_async(1, 2)  # should not raise


def test_handoff_async_records_mise_callback(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "shared-secret")
    callbacks: list[dict] = []

    def fake_recommend(gid, *, argus_run_id=None):
        return {
            "run_id": 12,
            "bundles": [{}],
            "bundle_count": 1,
            "estimated_total_cents": 5000,
            "review_url": "http://plutus:8030/runs/12",
            "pitch_url": "http://plutus:8030/runs/12/pitch.txt",
        }

    def fake_callback(gallery_id, **kwargs):
        callbacks.append({"gallery_id": gallery_id, **kwargs})

    monkeypatch.setattr(plutus_client, "recommend_mise_gallery", fake_recommend)
    monkeypatch.setattr("app.mise_client.plutus_callback", fake_callback)
    plutus_client.handoff_async(5, 9)
    import time

    time.sleep(0.2)
    assert len(callbacks) == 1
    assert callbacks[0]["gallery_id"] == 5
    assert callbacks[0]["run_id"] == 12
    assert callbacks[0]["review_url"] == "http://plutus:8030/runs/12"
    assert callbacks[0]["pitch_url"] == "http://plutus:8030/runs/12/pitch.txt"
    assert callbacks[0]["bundle_count"] == 1
    assert callbacks[0]["estimated_total_cents"] == 5000