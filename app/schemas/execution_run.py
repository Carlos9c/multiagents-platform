from pydantic import BaseModel, field_validator

from app.models.execution_run import (
    FAILURE_TYPE_EXECUTOR_REJECTED,
    FAILURE_TYPE_INTERNAL,
    FAILURE_TYPE_TRANSIENT,
    FAILURE_TYPE_UNKNOWN,
    FAILURE_TYPE_VALIDATION,
    RECOVERY_ACTION_MANUAL_REVIEW,
    RECOVERY_ACTION_NONE,
    RECOVERY_ACTION_REATOMIZE,
    RECOVERY_ACTION_RETRY_SAME_TASK,
    VALID_EXECUTION_RUN_STATUSES,
    VALID_FAILURE_TYPES,
    VALID_RECOVERY_ACTIONS,
)


class ExecutionRunBase(BaseModel):
    task_id: int
    parent_run_id: int | None = None
    agent_name: str
    attempt_number: int = 1
    status: str = "pending"
    input_snapshot: str | None = None
    output_snapshot: str | None = None
    error_message: str | None = None
    failure_type: str | None = None
    failure_code: str | None = None
    recovery_action: str | None = None
    work_summary: str | None = None
    work_details: str | None = None
    execution_agent_sequence: str | None = None
    artifacts_created: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None
    validation_notes: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in VALID_EXECUTION_RUN_STATUSES:
            raise ValueError(
                f"Invalid status '{value}'. Allowed values: {sorted(VALID_EXECUTION_RUN_STATUSES)}"
            )
        return value

    @field_validator("failure_type")
    @classmethod
    def validate_failure_type(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in VALID_FAILURE_TYPES:
            raise ValueError(
                f"Invalid failure_type '{value}'. Allowed values: {sorted(VALID_FAILURE_TYPES)}"
            )
        return value

    @field_validator("recovery_action")
    @classmethod
    def validate_recovery_action(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in VALID_RECOVERY_ACTIONS:
            raise ValueError(
                f"Invalid recovery_action '{value}'. "
                f"Allowed values: {sorted(VALID_RECOVERY_ACTIONS)}"
            )
        return value


class ExecutionRunCreate(ExecutionRunBase):
    pass


class ExecutionRunRead(ExecutionRunBase):
    id: int

    model_config = {"from_attributes": True}


__all__ = [
    "ExecutionRunBase",
    "ExecutionRunCreate",
    "ExecutionRunRead",
    "FAILURE_TYPE_TRANSIENT",
    "FAILURE_TYPE_VALIDATION",
    "FAILURE_TYPE_EXECUTOR_REJECTED",
    "FAILURE_TYPE_INTERNAL",
    "FAILURE_TYPE_UNKNOWN",
    "RECOVERY_ACTION_NONE",
    "RECOVERY_ACTION_RETRY_SAME_TASK",
    "RECOVERY_ACTION_REATOMIZE",
    "RECOVERY_ACTION_MANUAL_REVIEW",
]
