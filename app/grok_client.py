"""xAI Grok API client for Argus vision (replaces local Ollama/qwen)."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from . import config

log = logging.getLogger("argus.grok")

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"


class GrokVisionError(Exception):
    """Raised when the xAI API returns an error or unusable response."""


def _friendly_http_error(status_code: int, body: str) -> str:
    """Map common xAI HTTP errors to operator-friendly messages."""
    snippet = (body or "").strip()[:500]
    lower = snippet.lower()

    if status_code == 403:
        if "permission" in lower or "credit" in lower or "billing" in lower:
            return (
                "xAI permission denied — add credits for your team at "
                "https://console.x.ai (API key is valid but billing may be empty)"
            )
        return f"xAI forbidden (HTTP 403): {snippet}"

    if status_code == 429:
        return "xAI rate limit — wait and retry, or reduce concurrent vision jobs"

    if status_code == 400:
        if "model" in lower and ("not found" in lower or "invalid" in lower):
            return f"xAI model error — check ARGUS_VISION_MODEL ({config.VISION_MODEL}): {snippet}"
        return f"xAI bad request (HTTP 400): {snippet}"

    if status_code == 401:
        return "xAI unauthorized — rotate XAI_API_KEY and update .env"

    return f"xAI HTTP {status_code}: {snippet}"


def parse_usage(api_response: dict[str, Any]) -> dict[str, Any]:
    """Extract token usage and optional cost from an xAI chat completion."""
    usage = api_response.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)

    cost_usd: float | None = None
    for key in ("cost", "total_cost", "cost_usd"):
        raw = usage.get(key)
        if raw is not None:
            try:
                cost_usd = float(raw)
                break
            except (TypeError, ValueError):
                pass

    if cost_usd is None and config.COST_TRACKING:
        cost_usd = config.CLOUD_COST_PER_IMAGE

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }


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
        raise GrokVisionError(_friendly_http_error(resp.status_code, resp.text))

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise GrokVisionError(f"xAI returned non-JSON body: {resp.text[:300]}") from exc

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