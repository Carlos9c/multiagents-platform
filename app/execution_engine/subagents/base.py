from __future__ import annotations

from abc import ABC, abstractmethod

from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState


class SubagentExecutionError(Exception):
    """Base exception for subagent failures."""


class SubagentRejectedStepError(SubagentExecutionError):
    """Raised when a subagent cannot safely execute a specific step."""


class BaseSubagent(ABC):
    name: str

    @abstractmethod
    def supports_step_kind(self, step_kind: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def execute_step(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        raise NotImplementedError
