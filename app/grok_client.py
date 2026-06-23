"""xAI Grok API client for Argus vision (replaces local Ollama/qwen)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config

log = logging.getLogger("argus.grok")

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"


class GrokVisionError(Exception):
    """Raised when the xAI API returns an error or unusable response."""


def chat_vision(
    *,
    system_prompt: str,
    user_prompt: str,
    image_jpeg_b64: str,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Call Grok vision via xAI chat completions. Returns the raw API JSON."""
    key = api_key or config.XAI_API_KEY
    if not key:
        raise GrokVisionError("XAI_API_KEY is not set")

    model_name = model or config.VISION_MODEL
    data_url = f"data:image/jpeg;base64,{image_jpeg_b64}"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                    {"type": "text", "text": user_prompt},
                ],
            },
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "store": False,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=config.XAI_TIMEOUT) as client:
        resp = client.post(XAI_CHAT_URL, json=payload, headers=headers)

    if resp.status_code >= 400:
        raise GrokVisionError(f"xAI HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise GrokVisionError("xAI response missing choices")

    message = choices[0].get("message") or {}
    if message.get("refusal"):
        raise GrokVisionError(f"model refused: {message['refusal']}")

    return data


def message_json_text(message: dict[str, Any]) -> str:
    """Extract JSON text from an xAI assistant message."""
    for field in ("content", "reasoning_content"):
        text = (message.get(field) or "").strip()
        if text:
            return text
    return ""