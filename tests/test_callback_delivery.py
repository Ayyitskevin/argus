"""Reliable callback delivery (PR2): retry/backoff, dead-letter, re-delivery.

Mock backend + mocked httpx transport + no-op sleep — no live network, no waits."""

import os
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

_TMP = tempfile.mkdtemp(prefix="argus-cbdeliver-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_QUEUE_ENABLED"] = "false"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, mise_client  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
ADMIN = {"Authorization": "Bearer admin-tok"}

_PAYLOAD = {"idempotency_key": "argus-g7-r42", "run_id": 42, "gallery_id": 7, "status": "done"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "admin-tok")
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    monkeypatch.setattr(config, "MISE_URL", "http://mise.local")
    monkeypatch.setattr(config, "MISE_API_TOKEN", "tok")
    monkeypatch.setattr(config, "CAP_WEBHOOK_URL", None)
    monkeypatch.setattr(config, "MISE_CALLBACK_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("app.mise_client.time.sleep", lambda s: None)  # no real backoff waits
    db._SCHEMA_READY = False
    db.init()
    for row in db.list_dead_letter_callbacks(limit=1000):
        db.resolve_dead_letter_callback(row["idempotency_key"])
    yield


_REAL_HTTPX_CLIENT = httpx.Client  # capture before any patching so re-mocking nests correctly


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    class _Factory:
        def __init__(self, timeout=None, **kw):
            self._c = _REAL_HTTPX_CLIENT(transport=transport, timeout=timeout)

        def __enter__(self):
            return self._c.__enter__()

        def __exit__(self, *a):
            return self._c.__exit__(*a)

    monkeypatch.setattr("app.mise_client.httpx.Client", _Factory)


def _seq(steps):
    """Handler that returns/raises steps[i] on call i (last step repeats)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = calls["n"]
        calls["n"] += 1
        step = steps[min(i, len(steps) - 1)]
        if isinstance(step, Exception):
            raise step
        if step < 300:
            return httpx.Response(step, json={"ok": True}, request=request)
        return httpx.Response(step, text=f"err {step}", request=request)

    return handler, calls


# --- retry / backoff ----------------------------------------------------------

def test_transient_then_success_no_dead_letter(monkeypatch):
    handler, calls = _seq([503, 503, 200])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert calls["n"] == 3  # retried twice, delivered on the 3rd
    assert db.dead_letter_callback_count() == 0


def test_network_error_then_success(monkeypatch):
    handler, calls = _seq([httpx.ConnectError("refused"), 200])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert calls["n"] == 2
    assert db.dead_letter_callback_count() == 0


def test_exhausted_transient_dead_letters(monkeypatch):
    handler, calls = _seq([503])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert calls["n"] == 3  # max attempts
    rows = db.list_dead_letter_callbacks()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == "argus-g7-r42"
    assert rows[0]["run_id"] == 42


def test_hard_failure_no_retry_dead_letters(monkeypatch):
    handler, calls = _seq([401])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert calls["n"] == 1  # 401 is a hard failure — not retried (PR3 adds re-auth)
    assert db.dead_letter_callback_count() == 1


def test_stale_subject_is_noop(monkeypatch):
    handler, calls = _seq([404])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert calls["n"] == 1  # no retry
    assert db.dead_letter_callback_count() == 0  # no-op, not an error


def test_dead_letter_upsert_no_duplicate(monkeypatch):
    handler, _ = _seq([503])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)  # same key again
    rows = db.list_dead_letter_callbacks()
    assert len(rows) == 1  # one row per (gallery, run)
    assert rows[0]["attempts"] >= 2


def test_unconfigured_mise_is_noop(monkeypatch):
    monkeypatch.setattr(config, "MISE_URL", "")
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={}, request=request)

    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert called["n"] == 0
    assert db.dead_letter_callback_count() == 0


def test_dead_letter_fires_alert_webhook(monkeypatch):
    monkeypatch.setattr(config, "CAP_WEBHOOK_URL", "http://alert.local/hook")
    hits = {"callback": 0, "alert": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "alert.local":
            hits["alert"] += 1
            return httpx.Response(200, json={}, request=request)
        hits["callback"] += 1
        return httpx.Response(503, text="down", request=request)

    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert hits["alert"] == 1  # operator alerted on dead-letter
    assert db.dead_letter_callback_count() == 1


# --- re-delivery --------------------------------------------------------------

def test_redeliver_success_clears_outbox(monkeypatch):
    # seed a dead-letter
    handler, _ = _seq([503])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)
    assert db.dead_letter_callback_count() == 1

    # now Mise is healthy again
    ok_handler, calls = _seq([200])
    _mock(monkeypatch, ok_handler)
    summary = mise_client.redeliver_dead_letters()
    assert summary == {"attempted": 1, "delivered": 1, "still_failed": 0}
    assert db.dead_letter_callback_count() == 0


def test_redeliver_still_failing_keeps_row(monkeypatch):
    handler, _ = _seq([503])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)

    summary = mise_client.redeliver_dead_letters()
    assert summary["attempted"] == 1
    assert summary["still_failed"] == 1
    rows = db.list_dead_letter_callbacks()
    assert len(rows) == 1
    # attempts counts dead-letter/redelivery cycles: 1 (initial) + 1 (failed redeliver)
    assert rows[0]["attempts"] == 2


# --- admin endpoints ----------------------------------------------------------

def test_admin_endpoints(monkeypatch):
    handler, _ = _seq([503])
    _mock(monkeypatch, handler)
    mise_client.argus_callback(7, dict(_PAYLOAD), background=False)

    listed = client.get("/admin/callbacks/dead-letters", headers=ADMIN)
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    assert body["dead_letters"][0]["idempotency_key"] == "argus-g7-r42"
    assert "payload" not in body["dead_letters"][0]  # metadata only

    ok_handler, _ = _seq([200])
    _mock(monkeypatch, ok_handler)
    redeliver = client.post("/admin/callbacks/redeliver", headers=ADMIN)
    assert redeliver.status_code == 200
    assert redeliver.json()["delivered"] == 1
    assert db.dead_letter_callback_count() == 0


def test_admin_endpoints_require_auth():
    assert client.get("/admin/callbacks/dead-letters").status_code in (401, 403)
    assert client.post("/admin/callbacks/redeliver").status_code in (401, 403)
