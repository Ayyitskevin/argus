"""HEIC/RAW image_io helpers (no vision model calls)."""

import io
from unittest.mock import patch

from PIL import Image

from app import config, image_io, vision


def test_collect_folder_includes_raw_extensions(tmp_path):
    for name in ("scan.cr2", "plate.nef", "hero.jpg"):
        (tmp_path / name).write_bytes(b"\x00")
    found = {p.suffix.lower() for p in vision.collect_folder_images(tmp_path)}
    assert ".cr2" in found
    assert ".nef" in found
    assert ".jpg" in found


def test_prepare_jpeg_from_raster(tmp_path):
    path = tmp_path / "color.jpg"
    Image.new("RGB", (400, 300), color=(10, 20, 30)).save(path, format="JPEG")
    blob, (w, h) = image_io.prepare_jpeg_bytes(path)
    assert w == 400 and h == 300
    assert blob[:2] == b"\xff\xd8"


def test_exiftool_preview_fallback(tmp_path):
    raw = tmp_path / "frame.cr2"
    raw.write_bytes(b"not-a-real-raw")
    preview = Image.new("RGB", (200, 150), color=(90, 60, 30))
    buf = io.BytesIO()
    preview.save(buf, format="JPEG")
    fake_preview = buf.getvalue()

    with patch.object(image_io, "extract_preview_via_exiftool", return_value=fake_preview):
        blob, (w, h) = image_io.prepare_jpeg_bytes(raw)
    assert w == 200 and h == 150
    assert len(blob) > 100


def test_photo_exts_include_heic_and_raw():
    assert ".heic" in config.PHOTO_EXTS
    assert ".cr3" in config.PHOTO_EXTS