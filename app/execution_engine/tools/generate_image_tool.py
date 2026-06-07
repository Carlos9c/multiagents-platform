from __future__ import annotations

from app.services.image_providers.contracts import ImageGenerationRequest, ImageGenerationResult
from app.services.image_providers.factory import get_image_provider


def generate_image(request: ImageGenerationRequest) -> ImageGenerationResult:
    """
    Call the configured image provider and return the generation result.

    This tool does not write files — the caller is responsible for persisting
    the returned bytes to the workspace via write_binary_file.
    """
    provider = get_image_provider()
    return provider.generate(request)
