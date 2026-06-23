"""Unit tests for vision JSON extraction helpers (no API calls)."""

from app.vision import (
    _extract_json_blob,
    _grok_json_content,
    _is_degenerate_payload,
    _parse_vision_payload,
)


def test_grok_json_content_from_message():
    resp = {
        "choices": [
            {"message": {"content": '{"shot_type":"hero_plate","keywords":["a"]}'}}
        ]
    }
    assert "hero_plate" in _grok_json_content(resp)


def test_grok_json_content_falls_back_to_reasoning():
    resp = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": '{"shot_type":"detail_texture","keywords":["b"]}',
                }
            }
        ]
    }
    assert "detail_texture" in _grok_json_content(resp)


def test_extract_json_blob_from_prose():
    prose = 'Reasoning...\n{"shot_type":"hero_plate","keywords":["a"]}\nDone.'
    blob = _extract_json_blob(prose)
    assert "hero_plate" in blob


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