from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

DECISION_CALL_SUBAGENT = "call_subagent"
DECISION_FINISH = "finish"
DECISION_REJECT = "reject"

VALID_DECISION_TYPES = {
    DECISION_CALL_SUBAGENT,
    DECISION_FINISH,
    DECISION_REJECT,
}

VALID_SUBAGENT_NAMES = {
    "context_selection_agent",
    "code_change_agent",
    "command_runner_agent",
}


class NextActionDecision(BaseModel):
    decision_type: Literal[
        "call_subagent",
        "finish",
        "reject",
    ]
    rationale: str
    subagent_name: str | None = None
    target_paths: list[str] = Field(default_factory=list)
    expected_outcome: str | None = None
    risk_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "NextActionDecision":
        if self.decision_type == DECISION_CALL_SUBAGENT:
            if not self.subagent_name:
                raise ValueError("subagent_name is required when decision_type='call_subagent'.")
            if self.subagent_name not in VALID_SUBAGENT_NAMES:
                raise ValueError(
                    f"Unsupported subagent_name '{self.subagent_name}'. "
                    f"Allowed values: {sorted(VALID_SUBAGENT_NAMES)}"
                )
            return self

        if self.subagent_name is not None:
            raise ValueError("subagent_name must be null unless decision_type='call_subagent'.")

        return self
