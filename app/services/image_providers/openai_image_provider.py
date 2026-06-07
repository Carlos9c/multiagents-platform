from __future__ import annotations

import base64
import logging
import time

from openai import OpenAI

from app.services.image_providers.base import BaseImageProvider
from app.services.image_providers.contracts import (
    DALLE3_NATIVE_SIZES,
    ImageGenerationRequest,
    ImageGenerationResult,
)

logger = logging.getLogger("app.services.image_providers")

# DALL-E 3 size strings accepted by the API
_DALLE3_SIZE_MAP: dict[tuple[int, int], str] = {
    (1024, 1024): "1024x1024",
    (1792, 1024): "1792x1024",
    (1024, 1792): "1024x1792",
}


def _pick_native_size(width: int, height: int) -> tuple[int, int]:
    """Select the DALL-E 3 native resolution closest to the requested ratio."""
    if width == 0 or height == 0:
        return (1024, 1024)

    target_ratio = width / height
    best = min(
        DALLE3_NATIVE_SIZES,
        key=lambda wh: abs(wh[0] / wh[1] - target_ratio),
    )
    return best


class OpenAIImageProvider(BaseImageProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "dall-e-3",
        timeout: float = 120.0,
    ) -> None:
        self.client = OpenAI(api_key=api_key, timeout=timeout)
        self.model = model

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        started_at = time.perf_counter()

        native_w, native_h = _pick_native_size(request.width, request.height)
        size_str = _DALLE3_SIZE_MAP[(native_w, native_h)]

        # DALL-E 3 does not support negative_prompt as a separate param;
        # append it as a "avoid" clause inside the prompt when present.
        prompt = request.main_prompt
        if request.negative_prompt:
            prompt = f"{prompt}\n\nDo NOT include: {request.negative_prompt}"

        logger.info(
            "image_generation_started provider=openai model=%s size=%s prompt_chars=%d",
            self.model,
            size_str,
            len(prompt),
        )

        response = self.client.images.generate(
            model=self.model,
            prompt=prompt,
            size=size_str,
            response_format="b64_json",
            n=1,
        )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)

        image_data = response.data[0]
        b64_str = image_data.b64_json or ""
        if not b64_str:
            raise ValueError("OpenAI image generation returned empty b64_json.")

        image_bytes = base64.b64decode(b64_str)

        logger.info(
            "image_generation_completed provider=openai model=%s size=%s duration_ms=%d bytes=%d",
            self.model,
            size_str,
            elapsed_ms,
            len(image_bytes),
        )

        return ImageGenerationResult(
            image_bytes=image_bytes,
            model_used=self.model,
            seed_used=None,
            actual_width=native_w,
            actual_height=native_h,
            generation_duration_ms=elapsed_ms,
        )
