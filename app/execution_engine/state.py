from pydantic import BaseModel, Field


class ExecutionState(BaseModel):
    step_count: int = 0
    agent_call_count: int = 0
    tool_call_count: int = 0
    command_run_count: int = 0
    repair_attempt_count: int = 0
    visited_agents: list[str] = Field(default_factory=list)

    def register_step(self) -> None:
        self.step_count += 1

    def register_agent_call(self, agent_name: str) -> None:
        self.agent_call_count += 1
        self.visited_agents.append(agent_name)

    def register_tool_call(self) -> None:
        self.tool_call_count += 1

    def register_command_run(self) -> None:
        self.command_run_count += 1

    def register_repair_attempt(self) -> None:
        self.repair_attempt_count += 1