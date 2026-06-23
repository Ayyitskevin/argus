"""Phase 10 cloud vision provider adapters (Grok default, OpenAI/Anthropic optional).

Homelab mickey continues to use app.vision + Grok. SaaS tenants may select a
provider per tenant; mock backend always stays local for CI.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Callable

import httpx

from . import config, vision
from .grok_client import GrokVisionError, chat_vision, message_json_text, parse_usage
from .vision import (
    AnalysisResult,
    Culling,
    _parse_vision_payload,
    normalize_shot_type,
    system_prompt,
)

log = logging.getLogger("argus.cloud_vision")

PROVIDERS = ("grok", "openai", "anthropic", "mock")


class CloudVisionError(Exception):
    """Raised when a cloud vision provider fails."""


def resolve_provider(tenant: dict | None) -> str:
    if config.VISION_BACKEND == "mock":
        return "mock"
    if tenant and tenant.get("vision_provider"):
        return str(tenant["vision_provider"]).lower()
    return config.DEFAULT_VISION_PROVIDER


def analyze_with_provider(
    image_path: str | Path,
    *,
    provider: str,
    model: str | None = None,
    prefs: dict | None = None,
) -> tuple[AnalysisResult, dict]:
    """Analyze via provider. Returns (AnalysisResult, usage dict)."""
    provider = (provider or "grok").lower()
    if provider == "mock" or config.VISION_BACKEND == "mock":
        from . import metering

        result = vision._mock_result(image_path, None, None, model or config.VISION_MODEL)
        return vision._apply_prefs(result, prefs), {
            "cost_usd": metering.estimate_cost(1),
            "provider": "mock",
        }

    if provider == "grok":
        return _analyze_grok(image_path, model=model, prefs=prefs)
    if provider == "openai":
        return _analyze_openai(image_path, model=model, prefs=prefs)
    if provider == "anthropic":
        return _analyze_anthropic(image_path, model=model, prefs=prefs)
    raise CloudVisionError(f"unsupported vision provider: {provider}")


def _prepare_b64(image_path: str | Path) -> tuple[str, tuple[int, int]]:
    img_bytes, size = vision._prepare_image(image_path)
    return base64.b64encode(img_bytes).decode("utf-8"), size


def _build_result(
    image_path: str | Path,
    parsed: dict,
    *,
    model_label: str,
    width: int | None,
    height: int | None,
    prefs: dict | None,
) -> AnalysisResult:
    keywords = parsed.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k).strip() for k in keywords if str(k).strip()][: config.DEFAULT_MAX_TAGS]
    culling = parsed.get("culling") or {}
    if not isinstance(culling, dict):
        culling = {
            "keeper_score": 0.5,
            "hero_potential": 0.5,
            "technical_quality": "fair",
            "notes": str(culling),
        }
    result = AnalysisResult(
        image_path=str(image_path),
        width=width,
        height=height,
        shot_type=normalize_shot_type(parsed.get("shot_type")),
        keywords=keywords,
        culling=Culling(
            keeper_score=float(culling.get("keeper_score", 0.5)),
            hero_potential=float(culling.get("hero_potential", 0.5)),
            technical_quality=culling.get("technical_quality", "fair"),
            notes=culling.get("notes", ""),
        ),
        alt_text=(parsed.get("alt_text") or "").strip(),
        description=(parsed.get("description") or "").strip(),
        suggested_iptc=parsed.get("suggested_iptc") or {},
        raw_response=json.dumps(parsed, ensure_ascii=False),
        model=model_label,
    )
    return vision._apply_prefs(result, prefs)


def _analyze_grok(
    image_path: str | Path,
    *,
    model: str | None,
    prefs: dict | None,
) -> tuple[AnalysisResult, dict]:
    model_name = model or config.VISION_MODEL
    b64, (width, height) = _prepare_b64(image_path)
    user_prompt = vision.USER_PROMPT_TEMPLATE.format(max_tags=config.DEFAULT_MAX_TAGS)
    api_resp = chat_vision(
        system_prompt=system_prompt(),
        user_prompt=user_prompt,
        image_jpeg_b64=b64,
        model=model_name,
    )
    usage = parse_usage(api_resp)
    usage["provider"] = "grok"
    content = vision._grok_json_content(api_resp)
    parsed = _parse_vision_payload(content)
    result = _build_result(
        image_path,
        parsed,
        model_label=f"grok:{model_name}",
        width=width,
        height=height,
        prefs=prefs,
    )
    return result, usage


def _post_json_vision(
    *,
    url: str,
    headers: dict,
    payload: dict,
    extract_content: Callable[[dict], str],
    provider: str,
    model_label: str,
    image_path: str | Path,
    prefs: dict | None,
) -> tuple[AnalysisResult, dict]:
    with httpx.Client(timeout=config.XAI_TIMEOUT) as client:
        resp = client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise CloudVisionError(f"{provider} HTTP {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    content = extract_content(data)
    parsed = _parse_vision_payload(content)
    _, (width, height) = _prepare_b64(image_path)
    usage = {
        "provider": provider,
        "cost_usd": config.CLOUD_COST_PER_IMAGE if config.COST_TRACKING else 0.0,
    }
    result = _build_result(
        image_path,
        parsed,
        model_label=model_label,
        width=width,
        height=height,
        prefs=prefs,
    )
    return result, usage


def _analyze_openai(
    image_path: str | Path,
    *,
    model: str | None,
    prefs: dict | None,
) -> tuple[AnalysisResult, dict]:
    if not config.OPENAI_API_KEY:
        raise CloudVisionError("OPENAI_API_KEY is not set")
    model_name = model or config.OPENAI_VISION_MODEL
    b64, _ = _prepare_b64(image_path)
    user_prompt = vision.USER_PROMPT_TEMPLATE.format(max_tags=config.DEFAULT_MAX_TAGS)
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }

    def extract(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise CloudVisionError("openai response missing choices")
        return (choices[0].get("message") or {}).get("content") or ""

    return _post_json_vision(
        url="https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        payload=payload,
        extract_content=extract,
        provider="openai",
        model_label=f"openai:{model_name}",
        image_path=image_path,
        prefs=prefs,
    )


def _analyze_anthropic(
    image_path: str | Path,
    *,
    model: str | None,
    prefs: dict | None,
) -> tuple[AnalysisResult, dict]:
    if not config.ANTHROPIC_API_KEY:
        raise CloudVisionError("ANTHROPIC_API_KEY is not set")
    model_name = model or config.ANTHROPIC_VISION_MODEL
    b64, _ = _prepare_b64(image_path)
    user_prompt = vision.USER_PROMPT_TEMPLATE.format(max_tags=config.DEFAULT_MAX_TAGS)
    payload = {
        "model": model_name,
        "max_tokens": 2048,
        "system": system_prompt(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": user_prompt},
                ],
            }
        ],
    }

    def extract(data: dict) -> str:
        for block in data.get("content") or []:
            if block.get("type") == "text":
                return block.get("text") or ""
        raise CloudVisionError("anthropic response missing text block")

    return _post_json_vision(
        url="https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload=payload,
        extract_content=extract,
        provider="anthropic",
        model_label=f"anthropic:{model_name}",
        image_path=image_path,
        prefs=prefs,
    )