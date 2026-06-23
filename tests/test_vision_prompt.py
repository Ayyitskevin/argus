"""Vision prompt helpers — shot type normalization and examples loader."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from app.vision import load_prompt_examples, normalize_shot_type, system_prompt


def test_normalize_shot_type_aliases():
    assert normalize_shot_type("hero") == "hero_plate"
    assert normalize_shot_type("wide-establishing") == "wide_establishing"
    assert normalize_shot_type("unknown_thing") == "other"


def test_load_prompt_examples_from_file(monkeypatch, tmp_path):
    path = tmp_path / "examples.json"
    path.write_text(json.dumps({"examples": ["Example A", "Example B"]}), encoding="utf-8")
    monkeypatch.setenv("ARGUS_PROMPT_EXAMPLES_FILE", str(path))
    block = load_prompt_examples()
    assert "Example A" in block
    assert "Reference examples" in system_prompt()