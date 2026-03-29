from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


STEP_KIND_INSPECT_CONTEXT = "inspect_context"
STEP_KIND_RESOLVE_FILE_OPERATIONS = "resolve_file_operations"
STEP_KIND_APPLY_FILE_OPERATIONS = "apply_file_operations"
STEP_KIND_RUN_COMMAND = "run_command"
STEP_KIND_REPAIR = "repair"
STEP_KIND_SUMMARIZE = "summarize"

VALID_STEP_KINDS = {
    STEP_KIND_INSPECT_CONTEXT,
    STEP_KIND_RESOLVE_FILE_OPERATIONS,
    STEP_KIND_APPLY_FILE_OPERATIONS,
    STEP_KIND_RUN_COMMAND,
    STEP_KIND_REPAIR,
    STEP_KIND_SUMMARIZE,
}


class ExecutionStep(BaseModel):
    id: str
    kind: Literal[
        "inspect_context",
        "resolve_file_operations",
        "apply_file_operations",
        "run_command",
        "repair",
        "summarize",
    ]
    subagent_name: str
    title: str
    instructions: str
    target_paths: list[str] = Field(default_factory=list)
    command: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    task_id: int
    summary: str
    steps: list[ExecutionStep] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.steps) == 0
