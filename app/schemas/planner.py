from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.task_types import TaskType

Priority = Literal["high", "medium", "low"]


class PlannedTask(BaseModel):
    title: str = Field(min_length=10, max_length=255)
    description: str = Field(min_length=30)
    summary: str = Field(min_length=20)
    objective: str = Field(min_length=20)
    implementation_notes: str = Field(min_length=40)
    acceptance_criteria: list[str] = Field(min_length=1)
    technical_constraints: str = Field(min_length=10)
    out_of_scope: str = Field(min_length=10)
    priority: Priority
    task_type: TaskType


class PlannerOutput(BaseModel):
    plan_summary: str = Field(min_length=40)
    tasks: list[PlannedTask] = Field(min_length=4, max_length=10)
