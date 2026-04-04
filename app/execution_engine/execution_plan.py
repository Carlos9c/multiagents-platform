from __future__ import annotations

from pydantic import BaseModel, Field


class ExecutionStep(BaseModel):
    id: str
    subagent_name: str
    title: str
    instructions: str
    target_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    task_id: int
    summary: str
    steps: list[ExecutionStep] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.steps) == 0
