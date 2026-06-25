"""Stable folder signatures for Mise analyze dedup invalidation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import vision


def folder_fingerprint(folder: str | Path, *, recursive: bool = False) -> str:
    """Hash of supported image names, sizes, and mtimes in a folder."""
    root = Path(folder).expanduser().resolve()
    images = vision.collect_folder_images(root, recursive=recursive)
    if not images:
        return "empty"
    parts: list[str] = []
    for path in images:
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{stat.st_size}:{int(stat.st_mtime_ns)}")
    parts.sort()
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]