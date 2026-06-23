"""argus / photometa application package."""
from __future__ import annotations

from .client import ArgusClient, ArgusConfig  # type: ignore
from .mnemosyne_adapter import analyze_one, look_at_album  # type: ignore

try:
    from .sidecars import build_xmp, write_sidecar  # type: ignore
    from .vision import analyze_image, analyze_folder, AnalysisResult, Culling, make_thumbnail  # type: ignore
except Exception:
    analyze_image = analyze_folder = AnalysisResult = Culling = make_thumbnail = None  # type: ignore
    build_xmp = write_sidecar = None  # type: ignore
