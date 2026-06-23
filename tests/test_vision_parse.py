"""Unit tests for vision JSON extraction helpers (no Ollama)."""

from app.vision import _is_degenerate_payload, _ollama_json_content, _parse_vision_payload


def test_ollama_json_content_prefers_content():
    resp = {"message": {"content": '{"shot_type":"hero_plate"}', "thinking": "{}"}}
    assert "hero_plate" in _ollama_json_content(resp)


def test_ollama_json_content_falls_back_to_thinking():
    resp = {"message": {"content": "{}", "thinking": '{"shot_type":"detail_texture"}'}}
    assert "detail_texture" in _ollama_json_content(resp)


def test_degenerate_detects_empty_object():
    assert _is_degenerate_payload({}) is True


def test_degenerate_accepts_real_payload():
    payload = {
        "shot_type": "hero_plate",
        "keywords": ["rim light"],
        "alt_text": "Plated dish",
    }
    assert _is_degenerate_payload(payload) is False


def test_parse_vision_payload_requires_object():
    try:
        _parse_vision_payload("[]")
        raised = False
    except ValueError:
        raised = True
    assert raised