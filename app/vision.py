"""Vision analysis core for photometa / argus.

Uses xAI Grok API for image understanding (ARGUS_VISION_BACKEND=grok).
Mock backend stays local for CI. Produces structured JSON via response_format.
"""

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from . import config, metrics
from .grok_client import GrokVisionError, chat_vision, message_json_text, parse_usage


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

def make_thumbnail(path: str | Path, max_side: int = 512) -> bytes:
    """Return JPEG bytes of a downscaled thumbnail (orientation-corrected).

    Used by the /thumb endpoint to preview stored analyses without serving
    full-resolution originals."""
    import io
    with Image.open(Path(path)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


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


def _extract_json_blob(text: str) -> str:
    """Return a JSON object substring from model output (content or thinking)."""
    blob = (text or "").strip()
    if not blob or blob == "{}":
        return ""
    if blob.startswith("{") and blob.endswith("}"):
        return blob
    start = blob.find("{")
    end = blob.rfind("}")
    if start >= 0 and end > start:
        return blob[start : end + 1]
    return blob


def _grok_json_content(api_response: dict) -> str:
    """Pull JSON text from an xAI chat completion response."""
    choices = api_response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    blob = _extract_json_blob(message_json_text(message))
    return blob


def _parse_vision_payload(content: str) -> dict:
    if not (content or "").strip():
        raise ValueError("empty vision JSON")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("vision JSON root must be an object")
    return parsed


def _is_degenerate_payload(parsed: dict) -> bool:
    """True when the model returned technically-valid but useless JSON."""
    if not parsed:
        return True
    keywords = parsed.get("keywords") or []
    has_keywords = isinstance(keywords, list) and any(str(k).strip() for k in keywords)
    shot_type = (parsed.get("shot_type") or "").strip().lower()
    has_alt = bool((parsed.get("alt_text") or "").strip())
    return not has_keywords and shot_type in ("", "other") and not has_alt


def _apply_prefs(result: AnalysisResult, prefs: dict | None) -> AnalysisResult:
    """Nudge a result by learned preferences (Phase 3). Minimal but real: a
    culling_bias shifts keeper/hero scores (clamped 0..1) and keyword_boosts are
    prepended ahead of the model's own tags. No prefs -> result unchanged."""
    if not prefs:
        return result
    bias = float(prefs.get("culling_bias", 0.0) or 0.0)
    if bias:
        result.culling.keeper_score = max(0.0, min(1.0, result.culling.keeper_score + bias))
        result.culling.hero_potential = max(0.0, min(1.0, result.culling.hero_potential + bias))
    boosts = [str(b).strip() for b in (prefs.get("keyword_boosts") or []) if str(b).strip()]
    if boosts:
        existing = set(result.keywords)
        result.keywords = (
            [b for b in boosts if b not in existing] + result.keywords
        )[: config.DEFAULT_MAX_TAGS]

    preferred_type = (prefs.get("shot_type_preference") or "").strip().lower().replace(" ", "_")
    if preferred_type:
        if result.shot_type == preferred_type:
            result.culling.hero_potential = max(
                0.0, min(1.0, result.culling.hero_potential + 0.05)
            )
            result.culling.keeper_score = max(
                0.0, min(1.0, result.culling.keeper_score + 0.03)
            )
        elif result.shot_type == "other":
            result.shot_type = preferred_type
    return result


def _mock_result(
    image_path: str | Path, width: int | None, height: int | None, model: str
) -> AnalysisResult:
    """Synthetic analysis for VISION_BACKEND=mock — no model call. Deterministic
    (seeded by filename, so re-runs are stable) and shaped exactly like a real
    result, so every downstream path (DB, sidecars, exports, mnemosyne) can be
    exercised on a headless box or in CI without Ollama."""
    name = Path(image_path).stem
    seed = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16) % 100 / 100.0
    landscape = (width or 1) >= (height or 1)
    keeper = round(0.5 + 0.4 * seed, 2)
    return AnalysisResult(
        image_path=str(image_path),
        width=width,
        height=height,
        shot_type="hero_plate" if landscape else "portrait_subject",
        keywords=["mock", "f&b", "landscape" if landscape else "portrait", name],
        culling=Culling(
            keeper_score=keeper,
            hero_potential=round(keeper * 0.9, 2),
            technical_quality="good",
            notes="Mock analysis (ARGUS_VISION_BACKEND=mock); no vision model was called.",
        ),
        alt_text=f"Mock alt text for {name}.",
        description=f"Mock description for {name} ({width}x{height}).",
        suggested_iptc={
            "headline": name,
            "caption": f"Mock caption for {name}.",
            "keywords": ["mock", "f&b"],
        },
        raw_response="",
        model=f"mock:{model}",
    )


def analyze_image(
    image_path: str | Path,
    model: str | None = None,
    prefs: dict | None = None,
) -> AnalysisResult:
    """Run vision analysis on a single local image path. Returns typed AnalysisResult.

    Honors config.VISION_BACKEND: "mock" (CI default) or "grok"/"real" (xAI API).
    Optional learned `prefs` nudge the result in either mode."""
    model = model or config.VISION_MODEL
    img_bytes, (width, height) = _prepare_image(image_path)

    if config.VISION_BACKEND == "mock":
        return _apply_prefs(_mock_result(image_path, width, height, model), prefs)

    if config.VISION_BACKEND != "grok":
        raise ValueError(f"unsupported VISION_BACKEND: {config.VISION_BACKEND}")

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    user_prompt = USER_PROMPT_TEMPLATE.format(max_tags=config.DEFAULT_MAX_TAGS)

    try:
        def _chat_and_parse(extra_hint: str = "", *, temperature: float = 0.2) -> dict:
            prompt = user_prompt + (f"\n\n{extra_hint}" if extra_hint else "")
            api_resp = chat_vision(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt,
                image_jpeg_b64=b64,
                model=model,
                temperature=temperature,
            )
            metrics.record_grok_usage(parse_usage(api_resp))
            content = _grok_json_content(api_resp)
            return _parse_vision_payload(content)

        retry_hint = "Your previous reply was empty or invalid. Return one populated JSON object only."
        try:
            parsed = _chat_and_parse(temperature=0.2)
            if _is_degenerate_payload(parsed):
                log.warning("degenerate vision JSON for %s — retrying once", image_path)
                parsed = _chat_and_parse(retry_hint, temperature=0.1)
        except (json.JSONDecodeError, ValueError):
            log.warning("unparseable vision JSON for %s — retrying once", image_path)
            parsed = _chat_and_parse(retry_hint, temperature=0.1)

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

        result = AnalysisResult(
            image_path=str(image_path),
            width=width,
            height=height,
            shot_type=shot_type,
            keywords=keywords,
            culling=culling_obj,
            alt_text=(parsed.get("alt_text") or "").strip(),
            description=(parsed.get("description") or "").strip(),
            suggested_iptc=parsed.get("suggested_iptc") or {},
            raw_response=json.dumps(parsed, ensure_ascii=False),
            model=f"grok:{model}",
        )

        log.info("analyzed %s | model=%s | tags=%d | score=%.2f", image_path, model, len(keywords), result.culling.keeper_score)
        return _apply_prefs(result, prefs)

    except GrokVisionError as e:
        log.error("grok vision failed for %s: %s", image_path, e)
        err = str(e)
    except Exception as e:
        log.exception("vision analysis failed for %s", image_path)
        err = str(e)
    else:
        err = None

    if err is not None:
        # Return graceful fallback
        return AnalysisResult(
            image_path=str(image_path),
            width=width,
            height=height,
            shot_type="other",
            keywords=["analysis-failed"],
            culling=Culling(keeper_score=0.3, hero_potential=0.3, technical_quality="unknown", notes=f"Error: {err}"),
            alt_text="Image analysis unavailable.",
            description="",
            suggested_iptc={},
            raw_response=err,
            model=f"grok:{model}",
        )


def collect_folder_images(folder: str | Path, *, recursive: bool = False) -> list[Path]:
    """List supported image files in a folder (optionally recursive)."""
    root = Path(folder)
    if recursive:
        images = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in config.PHOTO_EXTS
        ]
    else:
        images = [
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in config.PHOTO_EXTS
        ]
    return sorted(images)


def analyze_folder(
    folder: str | Path,
    model: str | None = None,
    limit: int | None = None,
    prefs: dict | None = None,
    recursive: bool = False,
) -> list[AnalysisResult]:
    """Analyze supported images in a folder. Set recursive=True for nested galleries."""
    model = model or config.VISION_MODEL
    images = collect_folder_images(folder, recursive=recursive)
    if limit:
        images = images[:limit]

    results = []
    for img in images:
        try:
            res = analyze_image(img, model=model, prefs=prefs)
            results.append(res)
        except Exception as e:
            log.error("skipping %s: %s", img, e)
    return results
