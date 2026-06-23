"""Vision analysis core for photometa / argus.

Uses local Ollama + qwen3-vl (or configured model). Produces structured
photography-specific output via forced JSON.
"""

import base64
import json
import logging
from pathlib import Path
from typing import Any

import ollama
from PIL import Image, ImageOps

from . import config

log = logging.getLogger("argus.vision")

SYSTEM_PROMPT = """You are an expert professional photographer and photo editor who specializes in food & beverage, restaurant, and event photography. You have 15+ years of experience culling, keywording, and preparing images for client delivery, albums, licensing, and web use.

Your job is to produce precise, *actionable* structured metadata for the supplied photograph. Be specific and professional — avoid generic terms like "food on a plate". Use terminology a working photographer would actually write in a caption or keyword list.

Always return valid JSON only. No markdown, no explanations outside the JSON.
"""

USER_PROMPT_TEMPLATE = """Analyze this photograph in detail.

Return a single JSON object with exactly these keys:

{{
  "keywords": [string, ...],          // 8–{max_tags} highly specific photography keywords (composition, lighting, subject matter, mood, technical notes, story role). Use terms like "overhead flat lay", "rim lighting", "shallow depth of field", "detail shot of seared scallop", "candid guest moment".
  "culling": {{
    "keeper_score": 0.0–1.0,          // overall recommendation strength (higher = stronger candidate for use)
    "technical_quality": "excellent" | "good" | "fair" | "poor",
    "notes": "short professional note on focus, exposure, color, composition issues or strengths"
  }},
  "alt_text": "concise 1-sentence alt text suitable for web gallery (under 125 chars)",
  "description": "rich 2–4 sentence description a photo editor could use or adapt",
  "suggested_iptc": {{
    "headline": "short headline",
    "caption": "full caption ready for delivery",
    "keywords": [string, ...]           // IPTC-style keyword list (can overlap with top keywords)
  }}
}}

Prioritize usefulness for culling decisions and professional delivery. Be honest about technical shortcomings.
"""

def _prepare_image(path: str | Path) -> tuple[bytes, tuple[int, int]]:
    """Open, transpose orientation, convert, return bytes + (w, h)."""
    import io
    p = Path(path)
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), (w, h)


def analyze_image(image_path: str | Path, model: str | None = None) -> dict[str, Any]:
    """Run vision analysis on a single local image path. Returns structured dict + raw."""
    model = model or config.VISION_MODEL
    img_bytes, (width, height) = _prepare_image(image_path)
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    user_prompt = USER_PROMPT_TEMPLATE.format(max_tags=config.DEFAULT_MAX_TAGS)

    try:
        client = ollama.Client(host=config.OLLAMA_HOST)
        resp = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt, "images": [b64]},
            ],
            format="json",
            options={
                "temperature": 0.2,
                "top_p": 0.9,
            },
        )
        content = resp["message"]["content"].strip()
        parsed = json.loads(content)

        # Light normalization / defaults
        keywords = parsed.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k).strip() for k in keywords if str(k).strip()][: config.DEFAULT_MAX_TAGS]

        culling = parsed.get("culling") or {}
        if not isinstance(culling, dict):
            culling = {"keeper_score": 0.5, "technical_quality": "fair", "notes": str(culling)}

        result = {
            "image_path": str(image_path),
            "width": width,
            "height": height,
            "keywords": keywords,
            "culling": {
                "keeper_score": float(culling.get("keeper_score", 0.5)),
                "technical_quality": culling.get("technical_quality", "fair"),
                "notes": culling.get("notes", ""),
            },
            "alt_text": (parsed.get("alt_text") or "").strip(),
            "description": (parsed.get("description") or "").strip(),
            "suggested_iptc": parsed.get("suggested_iptc") or {},
            "raw_response": content,
            "model": model,
        }
        log.info("analyzed %s | model=%s | tags=%d | score=%.2f", image_path, model, len(keywords), result["culling"]["keeper_score"])
        return result

    except Exception as e:
        log.exception("vision analysis failed for %s", image_path)
        # Return graceful fallback so UI never explodes
        return {
            "image_path": str(image_path),
            "width": width,
            "height": height,
            "keywords": ["analysis-failed"],
            "culling": {"keeper_score": 0.3, "technical_quality": "unknown", "notes": f"Error: {e}"},
            "alt_text": "Image analysis unavailable.",
            "description": "",
            "suggested_iptc": {},
            "raw_response": str(e),
            "model": model,
        }


def analyze_folder(folder: str | Path, model: str | None = None, limit: int | None = None) -> list[dict]:
    """Analyze all supported images in a folder (non-recursive for Phase 0)."""
    model = model or config.VISION_MODEL
    p = Path(folder)
    images = sorted(
        f for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in config.PHOTO_EXTS
    )
    if limit:
        images = images[:limit]

    results = []
    for img in images:
        try:
            res = analyze_image(img, model=model)
            results.append(res)
        except Exception as e:
            log.error("skipping %s: %s", img, e)
    return results
