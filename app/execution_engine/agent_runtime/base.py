from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AgentRuntimeError(Exception):
    """Base exception for execution engine LLM runtime."""


class BaseAgentRuntime(ABC):
    @abstractmethod
    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError