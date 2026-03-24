from __future__ import annotations

from app.execution_engine.subagents.base import BaseSubagent


class SubagentRegistryError(Exception):
    """Raised when the registry cannot resolve a subagent."""


class SubagentRegistry:
    def __init__(self, subagents: list[BaseSubagent]) -> None:
        self._subagents = {subagent.name: subagent for subagent in subagents}

    def get(self, name: str) -> BaseSubagent:
        subagent = self._subagents.get(name)
        if not subagent:
            raise SubagentRegistryError(f"Subagent '{name}' is not registered")
        return subagent

    def all_names(self) -> list[str]:
        return sorted(self._subagents.keys())