from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    @abstractmethod
    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Execute one structured LLM call and return the parsed JSON object.

        Notes:
        - Implementations should raise on transport/provider failures.
        - Implementations should raise on empty or invalid structured responses.
        - Callers are responsible for any semantic/schema-level retry policy.
        """
        raise NotImplementedError