"""Vision pre-filter and parallel analyze."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-prefilter-")
os.environ["ARGUS_VISION_BACKEND"] = "mock"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, vision  # noqa: E402
from app.vision_prefilter import detect_non_photo  # noqa: E402


def test_detect_screenshot_filename():
    assert detect_non_photo("Screen Shot 2026-06-25.png", width=1200, height=800)[0]


def test_detect_small_png_without_exif(tmp_path, monkeypatch):
    path = tmp_path / "still.png"
    Image.new("RGB", (800, 600), (240, 240, 240)).save(path)
    monkeypatch.setattr("app.vision_prefilter._has_camera_exif", lambda p: False)
    is_junk, reason = detect_non_photo(path, width=800, height=600)
    assert is_junk
    assert "PNG" in reason


def test_real_photo_not_prefiltered(tmp_path):
    path = tmp_path / "DSC_1234.jpg"
    Image.new("RGB", (2400, 1600), (100, 80, 60)).save(path, quality=90)
    assert not detect_non_photo(path, width=2400, height=1600)[0]


def test_prefilter_skips_grok_call(tmp_path, monkeypatch):
    path = tmp_path / "Screenshot_001.png"
    Image.new("RGB", (600, 400), (200, 200, 200)).save(path)
    monkeypatch.setattr(config, "VISION_PREFILTER_ENABLED", True)
    monkeypatch.setattr(config, "VISION_BACKEND", "grok")
    monkeypatch.setattr(
        "app.vision.detect_non_photo",
        lambda *a, **k: (True, "test screenshot"),
    )

    def boom(*args, **kwargs):
        raise AssertionError("Grok should not be called for prefiltered image")

    monkeypatch.setattr("app.vision.chat_vision", boom)
    result = vision.analyze_image(path)
    assert result.model.startswith("prefilter:")
    assert result.culling.keeper_score < 0.15


def test_analyze_images_parallel_preserves_order(tmp_path, monkeypatch):
    folder = tmp_path / "batch"
    folder.mkdir()
    paths = []
    for i in range(4):
        p = folder / f"img{i}.jpg"
        Image.new("RGB", (20, 20), (i * 40, 0, 0)).save(p)
        paths.append(p)

    monkeypatch.setattr(config, "VISION_CONCURRENCY", 2)
    results = vision.analyze_images_parallel(paths)
    assert len(results) == 4
    assert [Path(r.image_path).name for r in results] == [p.name for p in paths]


def test_style_prompt_suffix_maps_known_styles():
    assert "food" in vision.style_prompt_suffix({"style": "f_and_b"}).lower()
    assert vision.style_prompt_suffix({"style": "custom-brand"}) == "Client style preference: custom-brand."