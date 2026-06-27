"""Callback contract hardening PR1 — idempotency key + correlation + status.

Mock backend only; the Idempotency-Key header test uses a mocked transport so no
live network call is made."""

import os
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-cbcontract-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, mise_client, service, structured_output  # noqa: E402
from app.auth_context import set_auth_context  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer cb-token"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "cb-token")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "MISE_URL", "")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "")
    monkeypatch.setattr(config, "STRUCTURED_OUTPUT_ENABLED", True)
    monkeypatch.setattr(config, "VISION_PREFILTER_ENABLED", False)
    db._SCHEMA_READY = False
    db.init()
    set_auth_context(None)
    yield
    set_auth_context(None)


def _gallery(tmp_path, gallery_id=1):
    originals = tmp_path / "media" / str(gallery_id) / "original"
    originals.mkdir(parents=True)
    Image.new("RGB", (80, 60), (200, 120, 40)).save(originals / "hero.jpg")
    Image.new("RGB", (60, 80), (40, 120, 200)).save(originals / "detail.jpg")
    return originals


# --- helpers ------------------------------------------------------------------

def test_idempotency_key_format_and_stability():
    assert structured_output.idempotency_key(7, 42) == "argus-g7-r42"
    # same (gallery, run) -> identical every time
    assert structured_output.idempotency_key(7, 42) == structured_output.idempotency_key(7, 42)
    # different run or gallery -> different key
    assert structured_output.idempotency_key(7, 42) != structured_output.idempotency_key(7, 43)
    assert structured_output.idempotency_key(8, 42) != structured_output.idempotency_key(7, 42)


@pytest.mark.parametrize(
    "raw,expected",
    [("done", "done"), ("DONE", "done"), ("queued", "queued"), ("error", "error"),
     ("", "done"), (None, "done"), ("weird", "done")],
)
def test_normalize_status(raw, expected):
    assert structured_output.normalize_status(raw) == expected


def test_build_callback_payload_has_key_and_status():
    payload = structured_output.build_callback_payload(
        {"photos": []}, gallery_id=5, run_id=9, status="DONE", correlation_id="c1"
    )
    assert payload["idempotency_key"] == "argus-g5-r9"
    assert payload["status"] == "done"
    assert payload["run_id"] == 9
    assert payload["correlation_id"] == "c1"


# --- end-to-end: emitted callback + endpoint --------------------------------

def test_emitted_callback_carries_key_and_correlation(monkeypatch, tmp_path):
    _gallery(tmp_path, gallery_id=7)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "media")
    captured = []
    monkeypatch.setattr(
        "app.mise_client.argus_callback",
        lambda gallery_id, payload, **kw: captured.append((gallery_id, payload)),
    )
    out = service.perform_folder_analyze(
        mise_gallery_id=7, client_id="mise", limit=0, correlation_id="corr-xyz"
    )
    run_id = int(out["run_id"])
    assert len(captured) == 1
    gid, payload = captured[0]
    assert gid == 7
    assert payload["idempotency_key"] == f"argus-g7-r{run_id}"
    assert payload["correlation_id"] == "corr-xyz"
    assert payload["status"] == "done"


def test_redelivery_key_is_stable(monkeypatch, tmp_path):
    _gallery(tmp_path, gallery_id=3)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "media")
    out = service.perform_folder_analyze(mise_gallery_id=3, client_id="mise", limit=0)
    run_id = int(out["run_id"])

    # The structured export = exactly what a (re)delivery would send. Two fetches
    # of the same run produce the identical idempotency key.
    a = client.get(f"/runs/{run_id}/structured", params={"gallery_id": 3}, headers=AUTH).json()
    b = client.get(f"/runs/{run_id}/structured", params={"gallery_id": 3}, headers=AUTH).json()
    assert a["idempotency_key"] == b["idempotency_key"] == f"argus-g3-r{run_id}"


def test_reanalyze_unchanged_gallery_keeps_same_key(monkeypatch, tmp_path):
    _gallery(tmp_path, gallery_id=4)
    monkeypatch.setattr(config, "MISE_MEDIA_ROOT", tmp_path / "media")
    first = service.perform_folder_analyze(mise_gallery_id=4, client_id="mise", limit=0)
    second = service.perform_folder_analyze(mise_gallery_id=4, client_id="mise", limit=0)
    # Re-analyze of the unchanged gallery is a dedupe hit -> same run_id -> same key.
    assert second.get("deduped") is True
    assert int(first["run_id"]) == int(second["run_id"])
    key = structured_output.idempotency_key(4, int(first["run_id"]))
    assert key == f"argus-g4-r{int(first['run_id'])}"


# --- HTTP layer: Idempotency-Key header --------------------------------------

def _mock_mise_httpx(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    class _Factory:
        def __init__(self, timeout=None, **kw):
            self._c = real(transport=transport, timeout=timeout)

        def __enter__(self):
            return self._c.__enter__()

        def __exit__(self, *a):
            return self._c.__exit__(*a)

    monkeypatch.setattr("app.mise_client.httpx.Client", _Factory)


def test_idempotency_key_sent_as_header(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://mise.local")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "tok")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_mise_httpx(monkeypatch, handler)
    payload = {"idempotency_key": "argus-g7-r42", "run_id": 42}
    mise_client.argus_callback(7, payload, background=False)
    assert seen["idem"] == "argus-g7-r42"
    assert seen["auth"] == "Bearer tok"


def test_no_idempotency_header_when_key_absent(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "http://mise.local")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "tok")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"ok": True}, request=request)

    _mock_mise_httpx(monkeypatch, handler)
    mise_client.argus_callback(7, {"run_id": 42}, background=False)
    assert seen["idem"] is None
