from typing import Literal

from pydantic import BaseModel, Field


TaskType = Literal[
    "requirements",
    "design",
    "planning",
    "implementation",
    "testing",
    "review",
    "documentation",
    "onboarding",
]

Priority = Literal["high", "medium", "low"]


class AtomicTaskOutput(BaseModel):
    title: str = Field(min_length=10, max_length=255)
    description: str = Field(min_length=20)
    summary: str = Field(min_length=15)
    objective: str = Field(min_length=15)
    proposed_solution: str = Field(min_length=30)
    implementation_steps: list[str] = Field(min_length=1, max_length=8)
    tests_required: list[str] = Field(min_length=1, max_length=8)
    acceptance_criteria: str = Field(min_length=20)
    technical_constraints: str = Field(min_length=10)
    out_of_scope: str = Field(min_length=10)
    priority: Priority
    task_type: TaskType


class AtomicTaskGenerationOutput(BaseModel):
    generation_summary: str = Field(min_length=30)
    atomic_tasks: list[AtomicTaskOutput] = Field(min_length=1, max_length=12)