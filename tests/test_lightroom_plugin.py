"""Phase 8 — Lightroom plugin bundle + export stub sanity checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "lightroom" / "Argus.lrplugin"
STUB = ROOT / "docs" / "lightroom_export_stub.py"


def test_lightroom_plugin_bundle_exists():
    assert (PLUGIN / "Info.lua").is_file()
    assert (PLUGIN / "ArgusFilter.lua").is_file()
    assert (PLUGIN / "PluginInit.lua").is_file()
    readme = ROOT / "plugins" / "lightroom" / "README.md"
    assert readme.is_file()
    assert "lightroom_export_stub.py" in readme.read_text(encoding="utf-8")


def test_export_stub_references_client_helpers():
    text = STUB.read_text(encoding="utf-8")
    assert "ArgusClient" in text
    assert "fetch_and_write_sidecars" in text
    assert "manifest" in text