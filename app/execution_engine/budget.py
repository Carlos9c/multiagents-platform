from pydantic import BaseModel


class LoopBudget(BaseModel):
    max_steps: int = 8
    max_agent_calls: int = 6
    max_tool_calls: int = 12
    max_command_runs: int = 4
    max_repair_attempts: int = 2