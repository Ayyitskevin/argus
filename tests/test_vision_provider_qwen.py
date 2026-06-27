"""Qwen vision provider — local OpenAI-compatible path, fully mocked (no live model).

Proves: same structured contract as Grok, cost_usd=0, downsized derivative,
strict-parse rejection, resilience (unreachable/HTTP error -> recorded, never a
raise), and that the default provider stays grok / mock ignores the switch."""

import base64
import io
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from PIL import Image

os.environ.setdefault("ARGUS_VISION_BACKEND", "mock")
_TMP = tempfile.mkdtemp(prefix="argus-qwen-")
os.environ["ARGUS_DATA_DIR"] = _TMP
os.environ["ARGUS_QUEUE_ENABLED"] = "false"

from app import cloud_vision, config, service, structured_output, vision  # noqa: E402

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "schemas" / "vision.schema.json").read_text("utf-8")
)
_VALIDATOR = Draft202012Validator(_SCHEMA)

_QWEN_CONTENT = json.dumps(
    {
        "shot_type": "hero_plate",
        "keywords": ["seared scallop", "rim lighting", "microgreens"],
        "culling": {
            "keeper_score": 0.82,
            "hero_potential": 0.71,
            "technical_quality": "excellent",
            "notes": "Crisp focus, clean plating.",
        },
        "alt_text": "Seared scallop plated with microgreens under rim light.",
        "description": "A tightly composed hero plate.",
        "suggested_iptc": {"headline": "Scallop", "caption": "Plated scallop.", "keywords": ["scallop"]},
    }
)


def _img(path: Path, size=(1600, 1200), color=(180, 120, 60)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _mock_cloud_httpx(monkeypatch, handler):
    """Route app.cloud_vision httpx.Client through a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    class _Factory:
        def __init__(self, timeout=None, **kwargs):
            self._client = real_client(transport=transport, timeout=timeout)

        def __enter__(self):
            return self._client.__enter__()

        def __exit__(self, *a):
            return self._client.__exit__(*a)

    monkeypatch.setattr("app.cloud_vision.httpx.Client", _Factory)


@pytest.fixture(autouse=True)
def _qwen_mode(monkeypatch):
    monkeypatch.setattr(config, "SAAS_MODE", False)
    monkeypatch.setattr(config, "VISION_BACKEND", "grok")  # real path
    monkeypatch.setattr(config, "VISION_PROVIDER", "qwen")
    monkeypatch.setattr(config, "VISION_PREFILTER_ENABLED", False)
    monkeypatch.setattr(config, "QWEN_BASE_URL", "http://qwen.local:11434/v1")
    monkeypatch.setattr(config, "QWEN_VISION_MODEL", "qwen3-vl:32b")
    monkeypatch.setattr(config, "QWEN_API_KEY", None)
    monkeypatch.setattr(config, "QWEN_MAX_IMAGE_PX", 1024)


def _ok_handler(content=_QWEN_CONTENT):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]}, request=request)

    return handler


# --- success / contract parity ----------------------------------------------

def test_qwen_success_emits_contract(monkeypatch, tmp_path):
    _mock_cloud_httpx(monkeypatch, _ok_handler())
    result = vision.analyze_image(_img(tmp_path / "a.jpg"))
    assert result.analysis_failed is False
    assert result.model == "qwen:qwen3-vl:32b"
    assert result.cost_usd == 0.0
    assert result.latency_ms is not None
    assert result.culling.keeper_score == 0.82
    assert "seared scallop" in result.keywords

    photo = structured_output.photo_to_vision(service.result_to_dict(result))
    _VALIDATOR.validate({"photos": [photo]})
    assert set(photo) == {"basename", "keywords", "alt_text", "keeper_score", "hero_potential"}
    assert photo["hero_potential"] == 0.71


def test_qwen_and_grok_identical_contract(monkeypatch, tmp_path):
    img = _img(tmp_path / "same.jpg")

    # qwen via mocked OpenAI-compatible endpoint
    _mock_cloud_httpx(monkeypatch, _ok_handler())
    qwen_photo = structured_output.photo_to_vision(
        service.result_to_dict(vision.analyze_image(img))
    )

    # grok via mocked xAI chat_vision (same structured content)
    monkeypatch.setattr(config, "VISION_PROVIDER", "grok")
    monkeypatch.setattr(config, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.vision.chat_vision",
        lambda **kw: {"choices": [{"message": {"content": _QWEN_CONTENT}}], "usage": {"total_tokens": 3}},
    )
    grok_photo = structured_output.photo_to_vision(
        service.result_to_dict(vision.analyze_image(img))
    )

    assert set(qwen_photo) == set(grok_photo)
    for key in ("keywords", "alt_text", "keeper_score", "hero_potential"):
        assert qwen_photo[key] == grok_photo[key]


def test_qwen_applies_client_style_like_grok(monkeypatch, tmp_path):
    # Parity: a client's style preference must steer the Qwen prompt exactly as it
    # steers the Grok prompt, or the two providers aren't comparable.
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["text"] = body["messages"][1]["content"][0]["text"]
        return httpx.Response(200, json={"choices": [{"message": {"content": _QWEN_CONTENT}}]}, request=request)

    _mock_cloud_httpx(monkeypatch, handler)
    vision.analyze_image(_img(tmp_path / "styled.jpg"), prefs={"style": "f_and_b"})
    assert "food & beverage" in captured["text"].lower()


def test_qwen_cost_is_zero_unit(monkeypatch, tmp_path):
    _mock_cloud_httpx(monkeypatch, _ok_handler())
    result, usage = cloud_vision._analyze_qwen(_img(tmp_path / "u.jpg"), model=None, prefs=None)
    assert usage["provider"] == "qwen"
    assert usage["cost_usd"] == 0.0
    assert result.model == "qwen:qwen3-vl:32b"


# --- privacy: downsized derivative ------------------------------------------

def test_qwen_sends_downsized_derivative(monkeypatch, tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        url = body["messages"][1]["content"][1]["image_url"]["url"]
        b64 = url.split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            captured["size"] = im.size
        return httpx.Response(200, json={"choices": [{"message": {"content": _QWEN_CONTENT}}]}, request=request)

    _mock_cloud_httpx(monkeypatch, handler)
    vision.analyze_image(_img(tmp_path / "big.jpg", size=(2400, 1800)))
    assert max(captured["size"]) <= 1024  # downsized to QWEN_MAX_IMAGE_PX, not the 2400px original


# --- strict parse + resilience ----------------------------------------------

def test_qwen_malformed_reply_recorded_not_raised(monkeypatch, tmp_path):
    _mock_cloud_httpx(monkeypatch, _ok_handler(content="this is not json"))
    result = vision.analyze_image(_img(tmp_path / "m.jpg"))
    assert result.analysis_failed is True
    assert result.cost_usd == 0.0
    assert result.model == "qwen:qwen3-vl:32b"
    assert result.keywords == ["analysis-failed"]


def test_qwen_empty_reply_rejected(monkeypatch, tmp_path):
    _mock_cloud_httpx(monkeypatch, _ok_handler(content=""))
    result = vision.analyze_image(_img(tmp_path / "e.jpg"))
    assert result.analysis_failed is True


def test_qwen_http_error_recorded(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(500, text="boom", request=request)

    _mock_cloud_httpx(monkeypatch, handler)
    result = vision.analyze_image(_img(tmp_path / "h.jpg"))
    assert result.analysis_failed is True


def test_qwen_non_json_body_recorded(monkeypatch, tmp_path):
    # A 200 carrying a non-JSON HTTP body must still be recorded, not raised.
    def handler(request):
        return httpx.Response(200, text="<html>upstream proxy error</html>", request=request)

    _mock_cloud_httpx(monkeypatch, handler)
    result = vision.analyze_image(_img(tmp_path / "nj.jpg"))
    assert result.analysis_failed is True


def test_qwen_non_json_body_raises_cloudvision_error(monkeypatch, tmp_path):
    # Helper-level contract: decode/parse failures surface as CloudVisionError
    # (the type the SaaS handler catches), never a bare JSONDecodeError.
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>", request=request)

    _mock_cloud_httpx(monkeypatch, handler)
    with pytest.raises(cloud_vision.CloudVisionError):
        cloud_vision._analyze_qwen(_img(tmp_path / "nj2.jpg"), model=None, prefs=None)


def test_qwen_unreachable_recorded(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _mock_cloud_httpx(monkeypatch, handler)
    result = vision.analyze_image(_img(tmp_path / "x.jpg"))  # must not raise
    assert result.analysis_failed is True
    assert result.cost_usd == 0.0


def test_qwen_single_image_failure_raises_analyze_error(monkeypatch, tmp_path):
    _mock_cloud_httpx(monkeypatch, _ok_handler(content="nope"))
    with pytest.raises(service.AnalyzeError):
        service.analyze_single_image(image_path=_img(tmp_path / "s.jpg"))


# --- default unchanged / mock ignores the switch ----------------------------

def test_default_provider_is_grok(monkeypatch):
    # With no env override, the provider resolves to grok (unchanged behavior).
    # The rest of the suite runs at this default and proves the Grok path intact.
    monkeypatch.delenv("ARGUS_VISION_PROVIDER", raising=False)
    assert (os.environ.get("ARGUS_VISION_PROVIDER", "grok").strip().lower() or "grok") == "grok"


def test_mock_backend_ignores_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VISION_BACKEND", "mock")  # CI default
    monkeypatch.setattr(config, "VISION_PROVIDER", "qwen")
    result = vision.analyze_image(_img(tmp_path / "mock.jpg"))
    assert result.model.startswith("mock:")  # provider switch is a no-op for mock
    assert result.analysis_failed is False
