"""Grok client tests — httpx mocked, no real API calls."""

import pytest

from app.grok_client import GrokVisionError, chat_vision, message_json_text


def test_message_json_text_prefers_content():
    msg = {"content": '{"ok": true}', "reasoning_content": ""}
    assert message_json_text(msg) == '{"ok": true}'


def test_chat_vision_requires_api_key(monkeypatch):
    monkeypatch.setattr("app.grok_client.config.XAI_API_KEY", None)
    with pytest.raises(GrokVisionError, match="XAI_API_KEY"):
        chat_vision(
            system_prompt="sys",
            user_prompt="user",
            image_jpeg_b64="abc",
        )