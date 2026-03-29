from app.core.config import settings
from app.services.llm.base import LLMProvider
from app.services.llm.openai_provider import OpenAIProvider


def get_llm_provider(model: str | None = None) -> LLMProvider:
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not configured.")

        selected_model = model or settings.openai_model

        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=selected_model,
        )

    raise ValueError(f"Unsupported llm provider: {settings.llm_provider}")
