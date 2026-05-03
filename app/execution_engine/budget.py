from pydantic import BaseModel


class LoopBudget(BaseModel):
    max_steps: int = 8
    max_agent_calls: int = 8
    max_repair_attempts: int = 2
