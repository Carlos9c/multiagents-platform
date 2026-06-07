from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.image_providers.contracts import ImageGenerationRequest
from app.services.image_providers.openai_image_provider import (
    OpenAIImageProvider,
    _pick_native_size,
)

# ── _pick_native_size ────────────────────────────────────────────────────────


def test_pick_native_size_square():
    assert _pick_native_size(128, 128) == (1024, 1024)


def test_pick_native_size_landscape():
    assert _pick_native_size(1920, 1080) == (1792, 1024)


def test_pick_native_size_portrait():
    assert _pick_native_size(800, 1400) == (1024, 1792)


def test_pick_native_size_zero_height():
    assert _pick_native_size(0, 0) == (1024, 1024)


# ── OpenAIImageProvider.generate ────────────────────────────────────────────


def _make_provider() -> OpenAIImageProvider:
    with patch("app.services.image_providers.openai_image_provider.OpenAI"):
        return OpenAIImageProvider(api_key="test-key", model="dall-e-3")


def _fake_b64_response(b64_data: str) -> MagicMock:
    img_data = MagicMock()
    img_data.b64_json = b64_data
    response = MagicMock()
    response.data = [img_data]
    return response


def test_generate_returns_result():
    import base64

    provider = _make_provider()
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    b64 = base64.b64encode(fake_bytes).decode()
    provider.client.images.generate.return_value = _fake_b64_response(b64)

    request = ImageGenerationRequest(
        main_prompt="A flat blue icon",
        width=128,
        height=128,
        output_format="png",
    )
    result = provider.generate(request)

    assert result.image_bytes == fake_bytes
    assert result.model_used == "dall-e-3"
    assert result.actual_width == 1024
    assert result.actual_height == 1024


def test_generate_appends_negative_prompt():
    import base64

    provider = _make_provider()
    fake_bytes = b"\x89PNG" + b"\x00" * 50
    b64 = base64.b64encode(fake_bytes).decode()
    provider.client.images.generate.return_value = _fake_b64_response(b64)

    request = ImageGenerationRequest(
        main_prompt="An icon",
        negative_prompt="no text",
        width=1024,
        height=1024,
        output_format="png",
    )
    provider.generate(request)

    call_kwargs = provider.client.images.generate.call_args[1]
    assert "Do NOT include: no text" in call_kwargs["prompt"]


def test_generate_raises_on_empty_response():
    provider = _make_provider()
    provider.client.images.generate.return_value = _fake_b64_response("")

    request = ImageGenerationRequest(
        main_prompt="test",
        width=512,
        height=512,
        output_format="png",
    )
    with pytest.raises(ValueError, match="empty b64_json"):
        provider.generate(request)
