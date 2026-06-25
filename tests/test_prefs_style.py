"""Client style suffix wiring for folder analyze."""

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="argus-style-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, db, service  # noqa: E402


def test_prefs_for_run_adds_style():
    assert service.prefs_for_run("client-a", style="f_and_b") == {"style": "f_and_b"}


def test_prefs_for_run_merges_existing(monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", Path(_TMP))
    monkeypatch.setattr(config, "DB_PATH", Path(_TMP) / "argus.db")
    db._SCHEMA_READY = False
    db.init()
    db.set_preferences("merge-client", {"keyword_boosts": ["steam"]})
    merged = service.prefs_for_run("merge-client", style="events")
    assert merged["style"] == "events"
    assert merged["keyword_boosts"] == ["steam"]