from __future__ import annotations

from typing import Any
from app.core.config import settings

from app.execution_engine.agent_runtime.base import AgentRuntimeError, BaseAgentRuntime
from app.services.llm.factory import get_llm_provider


class StructuredLLMRuntime(BaseAgentRuntime):
    """
    Provider-agnostic runtime over the existing app.services.llm abstraction.

    This keeps execution_engine independent from a concrete framework while still
    allowing structured reasoning with the currently configured provider/model.
    """

    def __init__(self, model: str | None = None) -> None:
        self.provider = get_llm_provider(model=settings.execution_engine_model)

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self.provider.generate_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name=schema_name,
                json_schema=json_schema,
            )
        except Exception as exc:
            raise AgentRuntimeError(
                f"Structured runtime failed for schema '{schema_name}': {str(exc)}"
            ) from exc
