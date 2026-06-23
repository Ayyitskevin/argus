"""Structured per-image vision logging."""

import logging

from PIL import Image

from app import config, vision


def test_analyze_image_emits_structured_event(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "VISION_BACKEND", "mock")
    monkeypatch.setattr(config, "STRUCTURED_LOGS", True)
    path = tmp_path / "log-me.jpg"
    Image.new("RGB", (640, 480), color=(40, 40, 40)).save(path, format="JPEG")

    caplog.set_level(logging.INFO, logger="argus.event")
    vision.analyze_image(path)

    records = [r.message for r in caplog.records if "vision.image_analyzed" in r.message]
    assert records
    assert '"latency_ms"' in records[0]
    assert '"path"' in records[0]
    assert str(path) in records[0]