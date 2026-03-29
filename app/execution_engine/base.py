from __future__ import annotations

from abc import ABC, abstractmethod

from app.execution_engine.budget import LoopBudget
from app.execution_engine.contracts import ExecutionRequest, ExecutionResult


class ExecutionEngineError(Exception):
    """Base exception for execution engine failures."""


class ExecutionEngineRejectedError(ExecutionEngineError):
    """Raised when the engine deliberately rejects a task."""

    def __init__(
        self,
        message: str,
        *,
        rejection_reason: str,
        remaining_scope: str | None = None,
        blockers_found: list[str] | None = None,
        validation_notes: list[str] | None = None,
        failure_code: str = "execution_engine_rejected",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.rejection_reason = rejection_reason
        self.remaining_scope = remaining_scope
        self.blockers_found = blockers_found or []
        self.validation_notes = validation_notes or []
        self.failure_code = failure_code


class BaseExecutionEngine(ABC):
    backend_name: str

    def __init__(self, budget: LoopBudget) -> None:
        self.budget = budget

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise NotImplementedError
