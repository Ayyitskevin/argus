"""argus / photometa application package (Phase 3).

Public API (for C - callable):
    from argus.vision import analyze_image, analyze_folder, AnalysisResult, Culling, make_thumbnail
    from argus.main import write_sidecar
    from argus.client import ArgusClient, ArgusConfig
    from argus.mnemosyne_adapter import analyze_one, look_at_album

Note: client + mnemosyne_adapter can be imported with minimal deps for integration use.
Full vision/main require server extras (fastapi, ollama, pillow...); see pyproject.
"""

from .client import ArgusClient, ArgusConfig  # type: ignore
from .mnemosyne_adapter import analyze_one, look_at_album  # type: ignore

# Heavy server/vision imports (optional for pure client/adapter consumers e.g. mnemosyne delegation)
try:
    from .vision import analyze_image, analyze_folder, AnalysisResult, Culling, make_thumbnail  # type: ignore
    from .main import write_sidecar
except Exception:
    # graceful for partial installs / client-only usage in the fleet
    analyze_image = analyze_folder = AnalysisResult = Culling = make_thumbnail = write_sidecar = None  # type: ignore
