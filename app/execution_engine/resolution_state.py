from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.execution_engine.context_selection import HistoricalTaskSelectionResult
from app.execution_engine.contracts import ExecutionEvidence, ExecutionRequest
from app.execution_engine.monitoring import OrchestratorTrace

ExecutionPhase = Literal[
    "discovery",
    "execution",
    "completion",
]


class ResolutionState(BaseModel):
    execution_request: ExecutionRequest

    selected_strategy: str | None = None
    active_step_id: str | None = None

    phase: ExecutionPhase = "discovery"

    historical_task_selection: HistoricalTaskSelectionResult | None = None

    materialization_attempt_count: int = 0

    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    step_notes: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)

    orchestrator_trace: OrchestratorTrace | None = None
    evidence: ExecutionEvidence = Field(default_factory=ExecutionEvidence)

    def replace_execution_request(self, request: ExecutionRequest) -> None:
        self.execution_request = request

    def set_historical_task_selection(
        self,
        selection: HistoricalTaskSelectionResult,
    ) -> None:
        self.historical_task_selection = selection

    def mark_step_completed(self, step_id: str) -> None:
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)

    def mark_step_failed(self, step_id: str) -> None:
        if step_id not in self.failed_steps:
            self.failed_steps.append(step_id)

    def add_note(self, note: str) -> None:
        self.step_notes.append(note)

    def add_risk_flags(self, risks: list[str]) -> None:
        for risk in risks:
            if risk not in self.risk_flags:
                self.risk_flags.append(risk)

    def increment_materialization_attempts(self) -> None:
        self.materialization_attempt_count += 1

    def mark_context_selected(self) -> None:
        self.phase = "execution"

    def has_outputs(self) -> bool:
        return bool(
            self.evidence.changed_files or self.evidence.commands or self.evidence.artifacts_created
        )
