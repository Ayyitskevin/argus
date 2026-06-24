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
        return {"run_id": 12, "bundles": [{}]}

    def fake_callback(
        gallery_id, *, run_id=None, status="done", error=None, offer_url=None
    ):
        callbacks.append(
            {
                "gallery_id": gallery_id,
                "run_id": run_id,
                "status": status,
                "error": error,
                "offer_url": offer_url,
            }
        )

    monkeypatch.setattr(plutus_client, "recommend_mise_gallery", fake_recommend)
    monkeypatch.setattr("app.mise_client.plutus_callback", fake_callback)
    plutus_client.handoff_async(5, 9)
    import time

    time.sleep(0.2)
    assert callbacks == [
        {
            "gallery_id": 5,
            "run_id": 12,
            "status": "done",
            "error": None,
            "offer_url": None,
        }
    ]


def test_create_share_link_posts(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8030")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "shared-secret")
    monkeypatch.setattr(config, "PLUTUS_TENANT_ID", None)
    captured: dict = {}

    class _Resp:
        def read(self):
            return json.dumps(
                {"public_url": "http://plutus:8030/store/studio/offer/tok1"}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode()
        return _Resp()

    with patch("app.plutus_client.urllib.request.urlopen", fake_urlopen):
        link = plutus_client.create_share_link(6, label="Wedding")

    assert link["public_url"].endswith("/offer/tok1")
    assert "run_id=6" in captured["body"]
    assert "label=Wedding" in captured["body"]
    assert captured["url"].endswith("/storefront/share-links")


def test_create_share_link_uses_integrations_when_tenant_set(monkeypatch):
    monkeypatch.setattr(config, "PLUTUS_URL", "http://plutus:8031")
    monkeypatch.setattr(config, "PLUTUS_TOKEN", "hook-secret")
    monkeypatch.setattr(config, "PLUTUS_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(config, "PLUTUS_TENANT_ID", "flow-studio")
    captured: dict = {}

    class _Resp:
        def read(self):
            return json.dumps(
                {"public_url": "http://plutus:8031/store/studio/offer/tok2"}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode()
        return _Resp()

    with patch("app.plutus_client.urllib.request.urlopen", fake_urlopen):
        link = plutus_client.create_share_link(7, label="Album")

    assert link["public_url"].endswith("/offer/tok2")
    assert captured["url"].endswith("/integrations/offer")
    assert "tenant_id=flow-studio" in captured["body"]