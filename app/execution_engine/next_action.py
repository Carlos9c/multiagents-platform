from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ACTION_INSPECT_CONTEXT = "inspect_context"
ACTION_RESOLVE_FILE_OPERATIONS = "resolve_file_operations"
ACTION_APPLY_FILE_OPERATIONS = "apply_file_operations"
ACTION_RUN_COMMAND = "run_command"
ACTION_FINISH = "finish"
ACTION_REJECT = "reject"

VALID_NEXT_ACTIONS = {
    ACTION_INSPECT_CONTEXT,
    ACTION_RESOLVE_FILE_OPERATIONS,
    ACTION_APPLY_FILE_OPERATIONS,
    ACTION_RUN_COMMAND,
    ACTION_FINISH,
    ACTION_REJECT,
}


class NextActionDecision(BaseModel):
    action: Literal[
        "inspect_context",
        "resolve_file_operations",
        "apply_file_operations",
        "run_command",
        "finish",
        "reject",
    ]
    rationale: str
    target_paths: list[str] = Field(default_factory=list)
    command: str | None = None
    expected_outcome: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
