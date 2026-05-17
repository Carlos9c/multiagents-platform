from app.core.config import settings
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMProvider
from app.services.llm.openai_provider import OpenAIProvider


def get_llm_provider(
    model: str | None = None,
    provider: str | None = None,
) -> LLMProvider:
    resolved_provider = provider or settings.llm_provider

    if resolved_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not configured.")
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=model or settings.openai_model,
        )

    if resolved_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured.")
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=model or settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
        )

    raise ValueError(f"Unsupported llm provider: {resolved_provider}")
