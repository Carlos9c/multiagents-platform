from pydantic import BaseModel


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
    executor_type: str = "code_executor"

    sequence_order: int | None = None

    status: str = "pending"

    is_blocked: bool = False
    blocking_reason: str | None = None


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

    sequence_order: int | None = None

    status: str

    is_blocked: bool
    blocking_reason: str | None = None

    model_config = {"from_attributes": True}