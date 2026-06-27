"""Vision analysis core for photometa / argus.

Uses xAI Grok API for image understanding (ARGUS_VISION_BACKEND=grok).
Mock backend stays local for CI. Produces structured JSON via response_format.
"""

import base64
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import config, metrics, structured_log
from .image_io import prepare_jpeg_bytes, thumbnail_jpeg_bytes
from .grok_client import GrokVisionError, chat_vision, message_json_text, parse_usage
from .vision_prefilter import detect_non_photo

STYLE_PROMPT_SUFFIXES: dict[str, str] = {
    "f_and_b": (
        "Client style: food & beverage and restaurant photography. Prioritize plating, "
        "steam, texture, bar cocktails, and environmental dining scenes."
    ),
    "events": (
        "Client style: event and candid coverage. Prioritize emotional moments, "
        "guest interaction, and venue atmosphere over static detail shots."
    ),
    "portrait": (
        "Client style: portrait and subject-focused work. Prioritize expression, "
        "connection, and clean backgrounds."
    ),
}


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
    analysis_failed: bool = False
    # Per-image accounting (Mise structured-output cost report). cost_usd is the
    # spend attributable to this image (real Grok/cloud usage, or simulated for
    # mock); latency_ms is wall time inside analyze_image. Both are summed per run.
    cost_usd: float | None = None
    latency_ms: float | None = None


log = logging.getLogger("argus.vision")

SHOT_TYPES = frozenset(
    {
        "wide_establishing",
        "environmental_medium",
        "hero_plate",
        "detail_texture",
        "candid_moment",
        "portrait_subject",
        "overhead_flatlay",
        "action_sequence",
        "table_scape",
        "other",
    }
)

SYSTEM_PROMPT = """You are an expert professional photographer and photo editor who specializes in food & beverage, restaurant, and event photography. You have 15+ years of experience culling, keywording, sequencing for albums, and preparing images for client delivery, licensing, and web use.

You evaluate images like a seasoned photo editor on a tight deadline:
- Lighting quality and mood
- Composition, depth, and framing
- Subject matter specificity (especially food plating, textures, restaurant environments, candid moments)
- Technical execution (focus, exposure, noise)
- Storytelling / album value (hero potential, sequence role, emotional impact)

Be extremely specific and professional. Never use generic language like "food on a table" or "nice photo". Use the exact kind of language a working F&B photographer would write in a shot list or album notes.

Non-photographic inputs (web UI screenshots, design mockups, placeholder gradients, video thumbnails, export dialogs) are NOT keeper images — assign shot_type "other", keeper_score below 0.15, and keywords that describe the interface (not invented food subjects).

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


def normalize_shot_type(raw: str | None) -> str:
    """Map model output to a known shot_type enum value."""
    value = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value in SHOT_TYPES:
        return value
    aliases = {
        "hero": "hero_plate",
        "plate": "hero_plate",
        "detail": "detail_texture",
        "texture": "detail_texture",
        "wide": "wide_establishing",
        "establishing": "wide_establishing",
        "environment": "environmental_medium",
        "environmental": "environmental_medium",
        "flatlay": "overhead_flatlay",
        "overhead": "overhead_flatlay",
        "portrait": "portrait_subject",
        "candid": "candid_moment",
        "tablescape": "table_scape",
    }
    return aliases.get(value, "other")


def load_prompt_examples() -> str:
    """Optional few-shot block from ARGUS_PROMPT_EXAMPLES_FILE (JSON list of strings)."""
    path_raw = os.environ.get("ARGUS_PROMPT_EXAMPLES_FILE", "").strip()
    if not path_raw:
        repo_root = Path(__file__).resolve().parent.parent
        for candidate in (
            repo_root / "examples" / "prompt_examples.json",
            config.DATA_DIR / "prompt_examples.json",
        ):
            if candidate.is_file():
                path_raw = str(candidate)
                break
        else:
            return ""
    path = Path(path_raw).expanduser()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not load prompt examples from %s: %s", path, exc)
        return ""
    if isinstance(data, dict):
        lines = data.get("examples") or data.get("few_shot") or []
    elif isinstance(data, list):
        lines = data
    else:
        return ""
    cleaned = [str(line).strip() for line in lines if str(line).strip()]
    if not cleaned:
        return ""
    return "\n\nReference examples (match this specificity and tone):\n" + "\n".join(
        f"- {line}" for line in cleaned[:6]
    )


def system_prompt() -> str:
    """System prompt with optional on-disk few-shot examples."""
    extra = load_prompt_examples()
    return SYSTEM_PROMPT + extra if extra else SYSTEM_PROMPT


def style_prompt_suffix(prefs: dict | None) -> str:
    """Optional per-client style block from prefs JSON (``style`` or ``client_style``)."""
    if not prefs:
        return ""
    raw = (prefs.get("style") or prefs.get("client_style") or "").strip().lower()
    if not raw:
        return ""
    mapped = STYLE_PROMPT_SUFFIXES.get(raw.replace(" ", "_").replace("-", "_"))
    if mapped:
        return mapped
    return f"Client style preference: {raw}."


def _prefiltered_result(
    image_path: str | Path,
    *,
    width: int | None,
    height: int | None,
    reason: str,
    model: str,
) -> AnalysisResult:
    name = Path(image_path).name
    return AnalysisResult(
        image_path=str(image_path),
        width=width,
        height=height,
        shot_type="other",
        keywords=["non-photographic", "prefiltered"],
        culling=Culling(
            keeper_score=0.05,
            hero_potential=0.02,
            technical_quality="poor",
            notes=f"Pre-filtered without vision API: {reason}",
        ),
        alt_text=f"Non-photographic file: {name}",
        description="Skipped vision analysis — likely screenshot, UI asset, or non-camera file.",
        suggested_iptc={"headline": name, "caption": reason, "keywords": ["non-photographic"]},
        raw_response=json.dumps({"prefiltered": True, "reason": reason}),
        model=f"prefilter:{model}",
        cost_usd=0.0,
    )


def make_thumbnail(path: str | Path, max_side: int = 512) -> bytes:
    """Return JPEG bytes of a downscaled thumbnail (orientation-corrected)."""
    return thumbnail_jpeg_bytes(path, max_side=max_side)


def _prepare_image(path: str | Path) -> tuple[bytes, tuple[int, int]]:
    """Open, transpose orientation, convert, return bytes + (w, h)."""
    return prepare_jpeg_bytes(path)


def _log_image_analyzed(
    *,
    image_path: str | Path,
    model: str,
    result: AnalysisResult | None,
    started: float,
    failed: bool = False,
) -> None:
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    # Stamp latency onto the result so every return path carries it without
    # having to thread the timer through each branch (used by the run cost report).
    if result is not None and result.latency_ms is None:
        result.latency_ms = latency_ms
    structured_log.event(
        "vision.image_analyzed",
        path=str(image_path),
        model=(result.model if result else model) or model,
        latency_ms=latency_ms,
        width=result.width if result else None,
        height=result.height if result else None,
        failed=failed or bool(result and result.analysis_failed),
        backend=config.VISION_BACKEND,
    )


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
        # Simulated per-image spend so the structured cost report is non-zero in
        # mock/CI without calling any model (mirrors service.simulated_cloud_cost).
        cost_usd=config.CLOUD_COST_PER_IMAGE if config.COST_TRACKING else 0.0,
    )


def _analyze_qwen_local(
    image_path: str | Path,
    *,
    model: str | None,
    prefs: dict | None,
    width: int | None,
    height: int | None,
    started: float,
) -> AnalysisResult:
    """Homelab/studio Qwen3-VL path.

    Reuses cloud_vision._analyze_qwen (same _build_result normalizer as Grok) so
    the output contract is identical, then enforces the resilience contract: any
    provider/parse/transport failure becomes a recorded analysis_failed result
    (cost 0), never a raise — the analyze/callback flow must not crash. Local
    Qwen is free, so cost_usd is always 0."""
    from . import cloud_vision

    # Callers thread the grok default (config.VISION_MODEL) as the model, so fall
    # back to the Qwen model unless an explicit non-default model was requested.
    model_name = model if (model and model != config.VISION_MODEL) else config.QWEN_VISION_MODEL
    try:
        result, _usage = cloud_vision._analyze_qwen(image_path, model=model_name, prefs=prefs)
        result.cost_usd = 0.0
        _log_image_analyzed(image_path=image_path, model=model_name, result=result, started=started)
        return result
    except Exception as exc:
        log.error("qwen vision failed for %s: %s", image_path, exc)
        failed = AnalysisResult(
            image_path=str(image_path),
            width=width,
            height=height,
            shot_type="other",
            keywords=["analysis-failed"],
            culling=Culling(
                keeper_score=0.3,
                hero_potential=0.3,
                technical_quality="unknown",
                notes=f"Error: {exc}",
            ),
            alt_text="Image analysis unavailable.",
            description="",
            suggested_iptc={},
            raw_response=str(exc),
            model=f"qwen:{model_name}",
            analysis_failed=True,
            cost_usd=0.0,
        )
        _log_image_analyzed(image_path=image_path, model=model_name, result=failed, started=started)
        return failed


def analyze_image(
    image_path: str | Path,
    model: str | None = None,
    prefs: dict | None = None,
    tenant: dict | None = None,
) -> AnalysisResult:
    """Run vision analysis on a single local image path. Returns typed AnalysisResult.

    Honors config.VISION_BACKEND: "mock" (CI default) or "grok"/"real" (xAI API).
    Optional learned `prefs` nudge the result in either mode."""
    started = time.perf_counter()
    model = model or config.VISION_MODEL
    img_bytes, (width, height) = _prepare_image(image_path)

    if config.VISION_PREFILTER_ENABLED:
        is_junk, reason = detect_non_photo(image_path, width=width, height=height)
        if is_junk:
            metrics.inc("vision_prefiltered")
            result = _apply_prefs(
                _prefiltered_result(
                    image_path,
                    width=width,
                    height=height,
                    reason=reason,
                    model=model,
                ),
                prefs,
            )
            _log_image_analyzed(image_path=image_path, model=model, result=result, started=started)
            return result

    if config.SAAS_MODE:
        from .auth_context import get_auth_context
        from . import cloud_vision, metering
        from .cloud_vision import CloudVisionError

        ctx = get_auth_context()
        tenant = tenant or (ctx.tenant if ctx else None)
        tenant_id = tenant["id"] if tenant else (ctx.tenant_id if ctx else None)
        provider = cloud_vision.resolve_provider(tenant)
        try:
            result, usage = cloud_vision.analyze_with_provider(
                image_path,
                provider=provider,
                model=model,
                prefs=prefs,
            )
            if usage.get("provider") == "grok":
                metrics.record_grok_usage(
                    {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                        "cost_usd": usage.get("cost_usd"),
                    }
                )
            metering.record_usage(
                tenant_id,
                images=1,
                cost_usd=usage.get("cost_usd"),
                grok_api_calls=1 if usage.get("provider") == "grok" else 0,
            )
            if result.cost_usd is None:
                result.cost_usd = usage.get("cost_usd")
            _log_image_analyzed(image_path=image_path, model=model, result=result, started=started)
            return result
        except CloudVisionError as exc:
            log.error("cloud vision failed for %s: %s", image_path, exc)
            err = str(exc)
            failed = AnalysisResult(
                image_path=str(image_path),
                width=width,
                height=height,
                shot_type="other",
                keywords=["analysis-failed"],
                culling=Culling(
                    keeper_score=0.3,
                    hero_potential=0.3,
                    technical_quality="unknown",
                    notes=f"Error: {err}",
                ),
                alt_text="Image analysis unavailable.",
                description="",
                suggested_iptc={},
                raw_response=err,
                model=f"{provider}:{model}",
                analysis_failed=True,
                cost_usd=0.0,
            )
            _log_image_analyzed(image_path=image_path, model=model, result=failed, started=started)
            return failed

    if config.VISION_BACKEND == "mock":
        mock = _apply_prefs(_mock_result(image_path, width, height, model), prefs)
        _log_image_analyzed(image_path=image_path, model=model, result=mock, started=started)
        return mock

    if config.VISION_BACKEND != "grok":
        raise ValueError(f"unsupported VISION_BACKEND: {config.VISION_BACKEND}")

    # Provider switch (reversible cutover). Default "grok" falls through to the
    # unchanged xAI path below; "qwen" routes to the local OpenAI-compatible
    # endpoint. Mock backend already returned above, so provider is real here.
    if config.VISION_PROVIDER == "qwen":
        return _analyze_qwen_local(
            image_path,
            model=model,
            prefs=prefs,
            width=width,
            height=height,
            started=started,
        )

    from .xai_budget import XaiBudgetError, check_budget, record_cost

    try:
        check_budget(images=1)
    except XaiBudgetError as exc:
        log.error("xAI budget blocked %s: %s", image_path, exc)
        failed = AnalysisResult(
            image_path=str(image_path),
            width=width,
            height=height,
            shot_type="other",
            keywords=["analysis-failed"],
            culling=Culling(
                keeper_score=0.0,
                hero_potential=0.0,
                technical_quality="unknown",
                notes=str(exc),
            ),
            alt_text="Image analysis unavailable.",
            description="",
            suggested_iptc={},
            raw_response=str(exc),
            model=f"grok:{model}",
            analysis_failed=True,
            cost_usd=0.0,
        )
        _log_image_analyzed(image_path=image_path, model=model, result=failed, started=started)
        return failed

    b64 = base64.b64encode(img_bytes).decode("utf-8")
    user_prompt = USER_PROMPT_TEMPLATE.format(max_tags=config.DEFAULT_MAX_TAGS)
    style_suffix = style_prompt_suffix(prefs)
    if style_suffix:
        user_prompt = f"{user_prompt}\n\n{style_suffix}"

    spent_usd = 0.0  # accumulated Grok spend for this image (includes retry calls)

    try:
        def _chat_and_parse(extra_hint: str = "", *, temperature: float = 0.2) -> dict:
            nonlocal spent_usd
            prompt = user_prompt + (f"\n\n{extra_hint}" if extra_hint else "")
            api_resp = chat_vision(
                system_prompt=system_prompt(),
                user_prompt=prompt,
                image_jpeg_b64=b64,
                model=model,
                temperature=temperature,
            )
            usage = parse_usage(api_resp)
            metrics.record_grok_usage(usage)
            call_cost = usage.get("cost_usd")
            if call_cost is not None:
                spent_usd += float(call_cost)
            record_cost(call_cost, image_path=str(image_path))
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

        shot_type = normalize_shot_type(parsed.get("shot_type"))

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
            cost_usd=spent_usd,
        )

        log.info("analyzed %s | model=%s | tags=%d | score=%.2f", image_path, model, len(keywords), result.culling.keeper_score)
        final = _apply_prefs(result, prefs)
        _log_image_analyzed(image_path=image_path, model=model, result=final, started=started)
        return final

    except GrokVisionError as e:
        log.error("grok vision failed for %s: %s", image_path, e)
        err = str(e)
    except Exception as e:
        log.exception("vision analysis failed for %s", image_path)
        err = str(e)
    else:
        err = None

    if err is not None:
        failed = AnalysisResult(
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
            analysis_failed=True,
            cost_usd=spent_usd,
        )
        _log_image_analyzed(image_path=image_path, model=model, result=failed, started=started)
        return failed


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


def analyze_images_parallel(
    images: list[Path],
    *,
    model: str | None = None,
    prefs: dict | None = None,
    tenant: dict | None = None,
    max_workers: int | None = None,
) -> list[AnalysisResult]:
    """Analyze a list of paths with bounded parallelism (order preserved)."""
    if not images:
        return []
    model = model or config.VISION_MODEL
    workers = max(1, min(max_workers or config.VISION_CONCURRENCY, len(images)))
    if workers == 1:
        out: list[AnalysisResult] = []
        for img in images:
            try:
                out.append(analyze_image(img, model=model, prefs=prefs, tenant=tenant))
            except Exception as exc:
                log.error("skipping %s: %s", img, exc)
        return out

    ordered: list[AnalysisResult | None] = [None] * len(images)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(analyze_image, img, model=model, prefs=prefs, tenant=tenant): idx
            for idx, img in enumerate(images)
        }
        for future in as_completed(futures):
            idx = futures[future]
            img = images[idx]
            try:
                ordered[idx] = future.result()
            except Exception as exc:
                log.error("skipping %s: %s", img, exc)
    return [item for item in ordered if item is not None]


def analyze_folder(
    folder: str | Path,
    model: str | None = None,
    limit: int | None = None,
    prefs: dict | None = None,
    recursive: bool = False,
    tenant: dict | None = None,
) -> list[AnalysisResult]:
    """Analyze supported images in a folder. Set recursive=True for nested galleries."""
    model = model or config.VISION_MODEL
    images = collect_folder_images(folder, recursive=recursive)
    if limit is not None and limit > 0:
        images = images[:limit]
    return analyze_images_parallel(images, model=model, prefs=prefs, tenant=tenant)
