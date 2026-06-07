from __future__ import annotations

from app.services.image_providers.base import BaseImageProvider


def get_image_provider(
    provider: str | None = None,
    model: str | None = None,
) -> BaseImageProvider:
    from app.core.config import settings

    resolved_provider = provider or settings.image_provider
    resolved_model = model or settings.image_model

    if resolved_provider == "openai":
        from app.services.image_providers.openai_image_provider import OpenAIImageProvider

        api_key = settings.openai_api_key
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for the openai image provider.")
        return OpenAIImageProvider(api_key=api_key, model=resolved_model)

    raise ValueError(f"Unknown image provider: {resolved_provider!r}. Supported: 'openai'.")
