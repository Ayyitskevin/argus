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
from pydantic import BaseModel, Field

from . import config


class Culling(BaseModel):
    keeper_score: float = Field(ge=0.0, le=1.0)
    hero_potential: float = Field(ge=0.0, le=1.0, default=0.5)
    technical_quality: str
    notes: str = ""


class AnalysisResult(BaseModel):
    image_path: str
    width: int | None = None
    height: int | None = None
    shot_type: str = "other"
    keywords: list[str] = Field(default_factory=list, max_length=20)
    culling: Culling
    alt_text: str = ""
    description: str = ""
    suggested_iptc: dict[str, Any] = Field(default_factory=dict)
    raw_response: str = ""
    model: str = ""


log = logging.getLogger("argus.vision")

SYSTEM_PROMPT = """You are an expert professional photographer and photo editor who specializes in food & beverage, restaurant, and event photography. You have 15+ years of experience culling, keywording, sequencing for albums, and preparing images for client delivery, licensing, and web use.

You evaluate images like a seasoned photo editor on a tight deadline:
- Lighting quality and mood
- Composition, depth, and framing
- Subject matter specificity (especially food plating, textures, restaurant environments, candid moments)
- Technical execution (focus, exposure, noise)
- Storytelling / album value (hero potential, sequence role, emotional impact)

Be extremely specific and professional. Never use generic language like "food on a table" or "nice photo". Use the exact kind of language a working F&B photographer would write in a shot list or album notes.

Always return valid JSON only. No markdown, no explanations outside the JSON.
"""

USER_PROMPT_TEMPLATE = """Analyze this photograph in detail.

Return **only** a single valid JSON object with exactly these keys:

{{
  "shot_type": "wide_establishing" | "environmental_medium" | "hero_plate" | "detail_texture" | "candid_moment" | "portrait_subject" | "overhead_flatlay" | "action_sequence" | "table_scape" | "other",
  "keywords": [string, ...],          // 8–{max_tags} highly specific photography terms. Prioritize: lighting (e.g. "rim lighting, steam, golden hour"), composition ("leading lines, negative space, shallow DOF"), subject specifics ("seared scallop with microgreens", "charred broccolini texture"), mood/story role.
  "culling": {{
    "keeper_score": 0.0–1.0,          // overall "keeper" strength for delivery or album (higher = much more likely to use)
    "hero_potential": 0.0–1.0,        // how strong this would be as a hero / spread anchor image
    "technical_quality": "excellent" | "good" | "fair" | "poor",
    "notes": "1-2 sentence professional editor note covering focus, exposure, color, distractions, and why it succeeds or fails"
  }},
  "alt_text": "concise 1-sentence alt text suitable for web gallery (under 125 chars, descriptive but natural)",
  "description": "rich 2–4 sentence description a photo editor could lift almost verbatim for proposals or captions",
  "suggested_iptc": {{
    "headline": "short punchy headline",
    "caption": "full caption ready for client delivery",
    "keywords": [string, ...]
  }}
}}

Focus especially on what would help an album designer (mnemosyne) decide sequencing and hero selection. Be brutally honest on technical issues. Use F&B-specific language.
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
            culling = {"keeper_score": 0.5, "hero_potential": 0.5, "technical_quality": "fair", "notes": str(culling)}

        shot_type = (parsed.get("shot_type") or "other").strip().lower().replace(" ", "_")

        culling_obj = Culling(
            keeper_score=float(culling.get("keeper_score", 0.5)),
            hero_potential=float(culling.get("hero_potential", 0.5)),
            technical_quality=culling.get("technical_quality", "fair"),
            notes=culling.get("notes", ""),
        )

        result_dict = {
            "image_path": str(image_path),
            "width": width,
            "height": height,
            "shot_type": shot_type,
            "keywords": keywords,
            "culling": culling_obj.model_dump(),
            "alt_text": (parsed.get("alt_text") or "").strip(),
            "description": (parsed.get("description") or "").strip(),
            "suggested_iptc": parsed.get("suggested_iptc") or {},
            "raw_response": content,
            "model": model,
        }

        # Validate with Pydantic (B step)
        try:
            validated = AnalysisResult(**result_dict)
            result = validated.model_dump()
        except Exception as ve:
            log.warning("Pydantic validation failed, using raw dict: %s", ve)
            result = result_dict

        log.info("analyzed %s | model=%s | tags=%d | score=%.2f", image_path, model, len(keywords), result["culling"]["keeper_score"])
        return result

    except Exception as e:
        log.exception("vision analysis failed for %s", image_path)
        # Return graceful fallback so UI never explodes
        return {
            "image_path": str(image_path),
            "width": width,
            "height": height,
            "shot_type": "other",
            "keywords": ["analysis-failed"],
            "culling": {"keeper_score": 0.3, "hero_potential": 0.3, "technical_quality": "unknown", "notes": f"Error: {e}"},
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
