"""Grok client tests — httpx mocked, no real API calls."""

import json

import httpx
import pytest

from app.grok_client import (
    GrokVisionError,
    chat_vision,
    message_json_text,
    parse_usage,
)


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


def test_parse_usage_extracts_tokens_and_cost(monkeypatch):
    monkeypatch.setattr("app.grok_client.config.COST_TRACKING", True)
    monkeypatch.setattr("app.grok_client.config.CLOUD_COST_PER_IMAGE", 0.002)
    usage = parse_usage(
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }
        }
    )
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 150
    assert usage["cost_usd"] == 0.002


def test_parse_usage_prefers_api_cost(monkeypatch):
    monkeypatch.setattr("app.grok_client.config.CLOUD_COST_PER_IMAGE", 0.001)
    usage = parse_usage({"usage": {"total_tokens": 10, "cost": 0.0042}})
    assert usage["cost_usd"] == 0.0042


def _mock_httpx_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    class _ClientFactory:
        def __init__(self, timeout=None, **kwargs):
            self._client = real_client(transport=transport, timeout=timeout)

        def __enter__(self):
            return self._client.__enter__()

        def __exit__(self, *args):
            return self._client.__exit__(*args)

    monkeypatch.setattr("app.grok_client.httpx.Client", _ClientFactory)


def test_friendly_403_permission_denied(monkeypatch):
    monkeypatch.setattr("app.grok_client.config.XAI_API_KEY", "test-key")
    monkeypatch.setattr("app.grok_client.config.VISION_MODEL", "grok-4-fast")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text='{"error":"permission denied for team"}',
            request=request,
        )

    _mock_httpx_client(monkeypatch, handler)

    with pytest.raises(GrokVisionError, match="credits"):
        chat_vision(
            system_prompt="sys",
            user_prompt="user",
            image_jpeg_b64="abc",
        )


def test_friendly_429_rate_limit(monkeypatch):
    monkeypatch.setattr("app.grok_client.config.XAI_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited", request=request)

    _mock_httpx_client(monkeypatch, handler)

    with pytest.raises(GrokVisionError, match="rate limit"):
        chat_vision(
            system_prompt="sys",
            user_prompt="user",
            image_jpeg_b64="abc",
        )


def test_chat_vision_success_returns_json(monkeypatch):
    monkeypatch.setattr("app.grok_client.config.XAI_API_KEY", "test-key")
    body = {
        "choices": [{"message": {"content": '{"shot_type":"hero_plate"}'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    _mock_httpx_client(monkeypatch, handler)

    data = chat_vision(
        system_prompt="sys",
        user_prompt="user",
        image_jpeg_b64="abc",
    )
    assert data["choices"][0]["message"]["content"] == '{"shot_type":"hero_plate"}'