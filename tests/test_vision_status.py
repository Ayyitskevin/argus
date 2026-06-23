"""Vision status endpoint and metrics wiring."""

import os
import tempfile

os.environ.setdefault("ARGUS_VISION_BACKEND", "mock")
_TMP = tempfile.mkdtemp(prefix="argus-vision-status-")
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_QUEUE_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient

from app import config, metrics
from app.grok_client import parse_usage
from app.main import app
from app.vision_status import vision_status

client = TestClient(app)


def test_vision_status_mock_ready(monkeypatch):
    monkeypatch.setattr(config, "VISION_BACKEND", "mock")
    status = vision_status()
    assert status["ready"] is True
    assert status["backend"] == "mock"
    assert status["provider"] == "mock"


def test_vision_status_grok_needs_key(monkeypatch):
    monkeypatch.setattr(config, "VISION_BACKEND", "grok")
    monkeypatch.setattr(config, "XAI_API_KEY", None)
    status = vision_status()
    assert status["ready"] is False
    assert "XAI_API_KEY" in status["message"]


def test_vision_status_endpoint():
    r = client.get("/vision/status")
    assert r.status_code == 200
    body = r.json()
    assert "backend" in body
    assert "ready" in body
    assert "grok_usage" in body


def test_record_grok_usage_updates_metrics():
    before = metrics.snapshot()
    metrics.record_grok_usage(parse_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}))
    after = metrics.snapshot()
    assert after["counters"]["grok_api_calls"] == before["counters"].get("grok_api_calls", 0) + 1
    assert after["counters"]["grok_total_tokens"] >= before["counters"].get("grok_total_tokens", 0) + 15