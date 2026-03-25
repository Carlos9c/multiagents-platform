from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.execution_engine.context_selection import ContextSelectionResult
from app.execution_engine.contracts import ExecutionEvidence
from app.execution_engine.file_operations import FileOperationPlan
from app.execution_engine.monitoring import OrchestratorTrace


ExecutionPhase = Literal[
    "discovery",
    "planning",
    "materialization",
    "completion",
]


class ResolutionState(BaseModel):
    selected_strategy: str | None = None
    active_step_id: str | None = None

    phase: ExecutionPhase = "discovery"

    observed_repo_summary: str | None = None
    candidate_paths: list[str] = Field(default_factory=list)
    selected_paths: list[str] = Field(default_factory=list)

    context_selection: ContextSelectionResult | None = None
    selected_file_context: str | None = None

    planned_file_operations: FileOperationPlan | None = None
    pending_operation_paths: list[str] = Field(default_factory=list)
    applied_operation_paths: list[str] = Field(default_factory=list)
    failed_operation_paths: list[str] = Field(default_factory=list)

    file_planning_attempt_count: int = 0
    materialization_attempt_count: int = 0

    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    step_notes: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)

    orchestrator_trace: OrchestratorTrace | None = None
    evidence: ExecutionEvidence = Field(default_factory=ExecutionEvidence)

    def mark_step_completed(self, step_id: str) -> None:
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)

    def mark_step_failed(self, step_id: str) -> None:
        if step_id not in self.failed_steps:
            self.failed_steps.append(step_id)

    def add_note(self, note: str) -> None:
        self.step_notes.append(note)

    def add_candidate_paths(self, paths: list[str]) -> None:
        for path in paths:
            if path not in self.candidate_paths:
                self.candidate_paths.append(path)

    def add_selected_paths(self, paths: list[str]) -> None:
        for path in paths:
            if path not in self.selected_paths:
                self.selected_paths.append(path)

    def add_risk_flags(self, risks: list[str]) -> None:
        for risk in risks:
            if risk not in self.risk_flags:
                self.risk_flags.append(risk)

    def increment_file_planning_attempts(self) -> None:
        self.file_planning_attempt_count += 1

    def increment_materialization_attempts(self) -> None:
        self.materialization_attempt_count += 1

    def set_planned_file_operations(self, plan: FileOperationPlan) -> None:
        self.planned_file_operations = plan
        self.pending_operation_paths = [item.path for item in plan.sorted_operations()]
        self.applied_operation_paths = []
        self.failed_operation_paths = []
        self.phase = "materialization"

    def mark_operation_applied(self, path: str) -> None:
        if path in self.pending_operation_paths:
            self.pending_operation_paths.remove(path)

        if path in self.failed_operation_paths:
            self.failed_operation_paths.remove(path)

        if path not in self.applied_operation_paths:
            self.applied_operation_paths.append(path)

        if not self.pending_operation_paths:
            self.phase = "completion"

    def mark_operation_failed(self, path: str) -> None:
        if path in self.pending_operation_paths:
            self.pending_operation_paths.remove(path)

        if path not in self.failed_operation_paths:
            self.failed_operation_paths.append(path)

    def mark_context_selected(self) -> None:
        self.phase = "planning"

    def has_pending_operations(self) -> bool:
        return len(self.pending_operation_paths) > 0

    def has_outputs(self) -> bool:
        return bool(self.evidence.changed_files or self.evidence.commands)

    def get_pending_plan_subset(self) -> FileOperationPlan | None:
        if self.planned_file_operations is None:
            return None

        pending = [
            item
            for item in self.planned_file_operations.sorted_operations()
            if item.path in self.pending_operation_paths
        ]

        return FileOperationPlan(
            summary=self.planned_file_operations.summary,
            operations=pending,
            assumptions=list(self.planned_file_operations.assumptions),
            risks=list(self.planned_file_operations.risks),
            notes=list(self.planned_file_operations.notes),
            rejection_reason=self.planned_file_operations.rejection_reason,
            remaining_scope=self.planned_file_operations.remaining_scope,
            blockers_found=list(self.planned_file_operations.blockers_found),
        )