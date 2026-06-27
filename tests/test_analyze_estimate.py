"""Preflight folder analyze estimates."""

import os
import tempfile

from PIL import Image

_TMP = tempfile.mkdtemp(prefix="argus-estimate-")
os.environ["ARGUS_VISION_BACKEND"] = "grok"
os.environ["ARGUS_DATA_DIR"] = _TMP

from app import config, service  # noqa: E402


def test_analyze_folder_estimate_counts_and_cost(tmp_path, monkeypatch):
    folder = tmp_path / "set"
    folder.mkdir()
    for i in range(2):
        Image.new("RGB", (10, 10), (i, 0, 0)).save(folder / f"p{i}.jpg")

    monkeypatch.setattr(config, "VISION_BACKEND", "grok")
    monkeypatch.setattr(config, "XAI_ESTIMATED_COST_PER_IMAGE", 0.02)
    monkeypatch.setattr(config, "XAI_DAILY_BUDGET_USD", 5.0)

    est = service.analyze_folder_estimate(folder, limit=0)
    assert est["image_count"] == 2
    assert est["analyze_all"] is True
    assert est["estimated_cost_usd"] == 0.04
    assert est["budget"]["enabled"] is True