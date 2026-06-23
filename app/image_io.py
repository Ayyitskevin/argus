"""Image open helpers — HEIC via pillow-heif, RAW previews via exiftool."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageOps

log = logging.getLogger("argus.image_io")

_HEIF_REGISTERED = False
_EXIFTOOL_PREVIEW_TAGS = (
    "PreviewImage",
    "JpgFromRaw",
    "ThumbnailImage",
    "OtherImage",
)


def register_heif_opener() -> bool:
    """Register HEIC/HEIF support when pillow-heif is installed."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        import pillow_heif  # type: ignore[import-untyped]

        pillow_heif.register_heif_opener()
        _HEIF_REGISTERED = True
        return True
    except ImportError:
        return False


def extract_preview_via_exiftool(path: Path) -> bytes | None:
    """Best-effort embedded JPEG preview for RAW files (requires exiftool on PATH)."""
    if not shutil.which("exiftool"):
        return None
    for tag in _EXIFTOOL_PREVIEW_TAGS:
        try:
            proc = subprocess.run(
                ["exiftool", "-b", f"-{tag}", str(path)],
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    return None


@contextmanager
def open_image(path: str | Path) -> Iterator[Image.Image]:
    """Open a raster or RAW file as a Pillow image (orientation not yet applied)."""
    register_heif_opener()
    p = Path(path)
    try:
        with Image.open(p) as im:
            yield im.copy()
        return
    except Exception as first_err:
        preview = extract_preview_via_exiftool(p)
        if preview:
            with Image.open(io.BytesIO(preview)) as im:
                yield im.copy()
            return
        raise first_err


def prepare_jpeg_bytes(path: str | Path, *, quality: int = 92) -> tuple[bytes, tuple[int, int]]:
    """Transpose, convert to RGB, return JPEG bytes and dimensions."""
    with open_image(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), (w, h)


def thumbnail_jpeg_bytes(path: str | Path, *, max_side: int = 512, quality: int = 85) -> bytes:
    with open_image(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()