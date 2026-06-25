"""Cheap heuristics to skip Grok on obvious non-photos (screenshots, tiny PNGs)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_SCREENSHOT_NAME = re.compile(
    r"(screenshot|screen[\s_-]?shot|screencap|ui[\s_-]|mockup|placeholder|"
    r"figma|canva|slack|discord|lightroom[\s_-]?catalog)",
    re.IGNORECASE,
)


def _has_camera_exif(path: Path) -> bool:
    """True when exiftool reports Make or Model (camera-origin signal)."""
    if not shutil.which("exiftool"):
        return True
    try:
        proc = subprocess.run(
            ["exiftool", "-Make", "-Model", "-s3", "-s3", str(path)],
            check=False,
            capture_output=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if proc.returncode != 0:
        return False
    lines = [line.strip() for line in proc.stdout.decode(errors="replace").splitlines()]
    return any(lines)


def detect_non_photo(
    path: str | Path,
    *,
    width: int | None = None,
    height: int | None = None,
) -> tuple[bool, str]:
    """Return (is_junk, reason). Conservative — only skips obvious non-photos."""
    p = Path(path)
    name = p.name
    stem = p.stem

    if _SCREENSHOT_NAME.search(name) or _SCREENSHOT_NAME.search(stem):
        return True, "filename suggests screenshot or UI asset"

    w = width or 0
    h = height or 0
    if w and h and max(w, h) < 320:
        return True, "very small image dimensions"

    ext = p.suffix.lower()
    if ext == ".png" and w and h and max(w, h) < 1400 and not _has_camera_exif(p):
        return True, "small PNG without camera EXIF"

    return False, ""