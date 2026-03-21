from pydantic import BaseModel


class ExecutionRunRead(BaseModel):
    id: int
    task_id: int
    agent_name: str
    status: str
    input_snapshot: str | None = None
    output_snapshot: str | None = None
    error_message: str | None = None

    model_config = {"from_attributes": True}