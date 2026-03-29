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


class PlannedTask(BaseModel):
    title: str = Field(min_length=10, max_length=255)
    description: str = Field(min_length=30)
    summary: str = Field(min_length=20)
    objective: str = Field(min_length=20)
    implementation_notes: str = Field(min_length=40)
    acceptance_criteria: str = Field(min_length=20)
    technical_constraints: str = Field(min_length=10)
    out_of_scope: str = Field(min_length=10)
    priority: Priority
    task_type: TaskType


class PlannerOutput(BaseModel):
    plan_summary: str = Field(min_length=40)
    tasks: list[PlannedTask] = Field(min_length=4, max_length=10)
