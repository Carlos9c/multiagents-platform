from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.task_types import TaskType

Priority = Literal["high", "medium", "low"]
VerificationLevel = Literal["none", "deferred", "runtime"]
ComplexityLevel = Literal["XS", "S", "M", "L", "XL"]


class AtomicTaskOutput(BaseModel):
    title: str = Field(min_length=10, max_length=255)
    description: str = Field(min_length=20)
    summary: str = Field(min_length=15)
    objective: str = Field(min_length=15)
    proposed_solution: str = Field(min_length=30)
    implementation_steps: list[str] = Field(min_length=1, max_length=8)
    tests_required: list[str] = Field(min_length=1, max_length=8)
    acceptance_criteria: list[str] = Field(min_length=1)
    technical_constraints: str = Field(min_length=10)
    out_of_scope: str = Field(min_length=10)
    priority: Priority
    task_type: TaskType
    verification_level: VerificationLevel
    estimated_complexity: ComplexityLevel
    depends_on_task_titles: list[str]


class AtomicTaskGenerationOutput(BaseModel):
    generation_summary: str = Field(min_length=30)
    atomic_tasks: list[AtomicTaskOutput] = Field(min_length=1, max_length=12)
