from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ImageFormat = Literal["png", "jpg", "webp"]
ResizeMode = Literal["fit", "fill", "crop"]

# DALL-E 3 native resolutions (width × height)
DALLE3_NATIVE_SIZES: list[tuple[int, int]] = [
    (1024, 1024),
    (1792, 1024),
    (1024, 1792),
]


class ImageGenerationRequest(BaseModel):
    main_prompt: str
    negative_prompt: str | None = None
    style_directive: str | None = None
    width: int
    height: int
    output_format: ImageFormat = "png"
    seed: int | None = None


class ImageGenerationResult(BaseModel):
    image_bytes: bytes
    model_used: str
    seed_used: int | None
    actual_width: int
    actual_height: int
    generation_duration_ms: int = Field(default=0)
