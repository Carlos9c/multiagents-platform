from __future__ import annotations

from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import STEP_KIND_RUN_COMMAND, ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.command_tool import CommandToolError, run_command


class CommandRunnerAgent(BaseSubagent):
    name = "command_runner_agent"

    def supports_step_kind(self, step_kind: str) -> bool:
        return step_kind == STEP_KIND_RUN_COMMAND

    def execute_step(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if not self.supports_step_kind(step.kind):
            raise SubagentRejectedStepError(f"{self.name} does not support step kind '{step.kind}'")

        if not step.command or not step.command.strip():
            raise SubagentRejectedStepError("Command step requires a non-empty command.")

        try:
            result = run_command(
                command=step.command,
                cwd=request.context.workspace_path,
            )
        except CommandToolError as exc:
            raise SubagentRejectedStepError(
                f"Command rejected by command policy: {str(exc)}"
            ) from exc

        state.evidence.commands.append(result)
        state.add_note(f"Command executed: {step.command} (exit_code={result.exit_code})")
        return state
