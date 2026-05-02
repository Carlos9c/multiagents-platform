from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

DECISION_CALL_SUBAGENT = "call_subagent"
DECISION_FINISH = "finish"
DECISION_REJECT = "reject"
DECISION_INVALID = "invalid"

VALID_SUBAGENT_NAMES = {
    "context_selection_agent",
    "code_change_agent",
    "command_runner_agent",
}


class NextActionDecision(BaseModel):
    decision_type: Literal[
        "invalid",
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
        self.rationale = self.rationale.strip()

        if not self.rationale:
            raise ValueError("rationale cannot be empty.")

        self.risk_flags = [
            flag.strip() for flag in self.risk_flags if isinstance(flag, str) and flag.strip()
        ]
        self.risk_flags = list(dict.fromkeys(self.risk_flags))

        self.target_paths = [
            path.strip() for path in self.target_paths if isinstance(path, str) and path.strip()
        ]

        if self.expected_outcome is not None:
            self.expected_outcome = self.expected_outcome.strip() or None

        if self.decision_type == DECISION_CALL_SUBAGENT:
            if not self.subagent_name:
                raise ValueError("subagent_name is required when decision_type='call_subagent'.")

            if self.subagent_name not in VALID_SUBAGENT_NAMES:
                raise ValueError(
                    f"Unsupported subagent_name '{self.subagent_name}'. "
                    f"Allowed values: {sorted(VALID_SUBAGENT_NAMES)}"
                )

            return self

        # For finish/reject/invalid: strip rather than reject.
        # OpenAI strict mode requires all fields to be present in the JSON response,
        # so the LLM legitimately echoes subagent_name/target_paths even when irrelevant.
        self.subagent_name = None
        self.target_paths = []
        return self
