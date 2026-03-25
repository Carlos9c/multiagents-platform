from pydantic import BaseModel, field_validator

from app.models.task import (
    PENDING_ENGINE_ROUTING_EXECUTOR,
    VALID_EXECUTOR_TYPES,
    VALID_PLANNING_LEVELS,
    VALID_TASK_STATUSES,
)


class TaskCreate(BaseModel):
    project_id: int
    parent_task_id: int | None = None
    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None
    proposed_solution: str | None = None
    implementation_notes: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    priority: str = "medium"
    task_type: str = "implementation"
    planning_level: str = "high_level"
    executor_type: str = PENDING_ENGINE_ROUTING_EXECUTOR
    sequence_order: int | None = None
    status: str = "pending"
    is_blocked: bool = False
    blocking_reason: str | None = None

    @field_validator("planning_level")
    @classmethod
    def validate_planning_level(cls, value: str) -> str:
        if value not in VALID_PLANNING_LEVELS:
            raise ValueError(
                f"Invalid planning_level '{value}'. "
                f"Allowed values: {sorted(VALID_PLANNING_LEVELS)}"
            )
        return value

    @field_validator("executor_type")
    @classmethod
    def validate_executor_type(cls, value: str) -> str:
        if value not in VALID_EXECUTOR_TYPES:
            raise ValueError(
                f"Invalid executor_type '{value}'. "
                f"Allowed values: {sorted(VALID_EXECUTOR_TYPES)}"
            )
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in VALID_TASK_STATUSES:
            raise ValueError(
                f"Invalid status '{value}'. "
                f"Allowed values: {sorted(VALID_TASK_STATUSES)}"
            )
        return value


class TaskRead(BaseModel):
    id: int
    project_id: int
    parent_task_id: int | None = None
    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None
    proposed_solution: str | None = None
    implementation_notes: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    priority: str
    task_type: str
    planning_level: str
    executor_type: str
    last_execution_agent_sequence: str | None = None
    sequence_order: int | None = None
    status: str
    is_blocked: bool
    blocking_reason: str | None = None

    model_config = {"from_attributes": True}