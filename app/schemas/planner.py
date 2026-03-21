from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    documentation_required: bool = True
    tasks: list[PlannedTask] = Field(min_length=4, max_length=10)

    @model_validator(mode="after")
    def validate_required_task_types(self):
        task_types = {task.task_type for task in self.tasks}

        if "documentation" not in task_types:
            raise ValueError("Planner output must include at least one documentation task.")

        if "onboarding" not in task_types:
            raise ValueError("Planner output must include at least one onboarding task.")

        if "implementation" not in task_types:
            raise ValueError("Planner output must include at least one implementation task.")

        return self