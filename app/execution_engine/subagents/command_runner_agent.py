from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.command_tool import CommandToolError, run_command
from app.execution_engine.tools.workspace_scan_tool import list_workspace_files
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import ProjectStorageService
from app.services.workspace_runtime import WorkspaceRuntimeError

COMMAND_RUNNER_AGENT_SYSTEM_PROMPT = """
You are a repository-local verification command planner.

Your job is to inspect the candidate run tree for ONE already-atomic task and decide:
- which single concrete command should be executed
- from which working directory inside the candidate run tree it should be executed
- what that command is meant to verify for later external validation

Return ONLY JSON matching the provided schema.

Hard rules:
- Choose exactly one concrete command.
- The command must be repo-local and narrow in purpose.
- Do not use shell chaining, pipes, redirection, or multiple commands.
- Prefer project-standard commands already supported by the repository layout.
- Use the smallest useful verification command.
- The working directory must be "." or a relative path inside the candidate run tree.
- Do not invent tools, executables, frameworks, entrypoints, or files that are not grounded in the provided inventory/context.
- The goal is to produce operational evidence for external validation, not to perform open-ended exploration.
""".strip()


class CommandVerificationPlan(BaseModel):
    command: str
    cwd_relative_path: str = "."
    verification_goal: str
    rationale: str
    validation_claims: list[str] = Field(default_factory=list)
    expected_exit_codes: list[int] = Field(default_factory=lambda: [0])


def _build_run_tree_inventory(run_dir: Path, *, max_files: int = 500) -> list[str]:
    return list_workspace_files(str(run_dir), max_files=max_files)


def _build_command_planning_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
) -> str:
    inventory = _build_run_tree_inventory(run_dir)
    inventory_text = "\n".join(f"- {path}" for path in inventory) if inventory else "[empty]"

    changed_files = [item.model_dump() for item in state.evidence.changed_files]
    files_read = [item.model_dump() for item in state.evidence.files_read]
    commands = [item.model_dump() for item in state.evidence.commands]

    return f"""
Task:
- task_id: {request.task_id}
- title: {request.task_title}
- description: {request.task_description}
- objective: {request.objective}
- acceptance_criteria: {request.acceptance_criteria}
- technical_constraints: {request.technical_constraints}
- out_of_scope: {request.out_of_scope}
- tests_required: {request.tests_required}

Command-step context:
- orchestrator_rationale: {step.instructions}

Candidate run tree root:
- absolute_path: {str(run_dir)}

Candidate run tree inventory:
{inventory_text}

Accumulated execution evidence so far:
- changed_files: {changed_files}
- files_read: {files_read}
- prior_commands: {commands}
- risk_flags: {state.risk_flags}
- step_notes: {state.step_notes}

Planning instructions:
- Decide the single most useful repository-local verification command to run now.
- The command must help external validation verify the task without re-running commands later.
- Choose the working directory relative to the candidate run tree.
- Ground the command strictly in the provided run-tree inventory, task context, and accumulated evidence.
- Prefer the smallest useful verification command.
""".strip()


def _resolve_command_cwd(run_dir: Path, cwd_relative_path: str) -> Path:
    relative = (cwd_relative_path or ".").strip() or "."
    candidate = (run_dir / relative).resolve()

    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise SubagentRejectedStepError(
            f"Planned command cwd escapes the candidate run tree: {cwd_relative_path}"
        ) from exc

    if not candidate.exists():
        raise SubagentRejectedStepError(
            f"Planned command cwd does not exist inside the candidate run tree: {cwd_relative_path}"
        )
    if not candidate.is_dir():
        raise SubagentRejectedStepError(
            f"Planned command cwd is not a directory: {cwd_relative_path}"
        )

    return candidate


class CommandRunnerAgent(BaseSubagent):
    name = "command_runner_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime
        self.workspace_runtime = LocalWorkspaceRuntime(storage_service=ProjectStorageService())

    def _plan_command(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
        run_dir: Path,
    ) -> CommandVerificationPlan:
        schema = to_openai_strict_json_schema(CommandVerificationPlan.model_json_schema())
        raw = self.runtime.generate_structured(
            system_prompt=COMMAND_RUNNER_AGENT_SYSTEM_PROMPT,
            user_prompt=_build_command_planning_prompt(
                request=request,
                step=step,
                state=state,
                run_dir=run_dir,
            ),
            schema_name="execution_engine_command_verification_plan",
            json_schema=schema,
        )

        try:
            return CommandVerificationPlan.model_validate(raw)
        except ValidationError as exc:
            raise SubagentRejectedStepError(
                f"Invalid command verification plan output: {str(exc)}"
            ) from exc

    def execute_step(
        self,
        *,
        db: Session,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
    ) -> ResolutionState:
        if step.subagent_name != self.name:
            raise SubagentRejectedStepError(
                f"{self.name} received a step for subagent '{step.subagent_name}'."
            )

        overlay_paths = [item.path for item in state.evidence.changed_files] or None
        run_dir: Path | None = None

        try:
            run_dir = self.workspace_runtime.materialize_run_tree(
                project_id=request.project_id,
                execution_run_id=request.execution_run_id,
                overlay_paths=overlay_paths,
            )

            plan = self._plan_command(
                request=request,
                step=step,
                state=state,
                run_dir=run_dir,
            )

            if not plan.command or not plan.command.strip():
                raise SubagentRejectedStepError(
                    "Command verification plan returned an empty command."
                )

            command_cwd = _resolve_command_cwd(run_dir, plan.cwd_relative_path)

            result = run_command(
                command=plan.command,
                cwd=str(command_cwd),
            )

        except WorkspaceRuntimeError as exc:
            raise SubagentRejectedStepError(
                f"Could not materialize ephemeral execution tree for command step: {str(exc)}"
            ) from exc
        except CommandToolError as exc:
            raise SubagentRejectedStepError(
                f"Command rejected by command policy: {str(exc)}"
            ) from exc
        finally:
            try:
                self.workspace_runtime.cleanup_run_tree(
                    project_id=request.project_id,
                    execution_run_id=request.execution_run_id,
                )
            except Exception:
                pass

        observed_outcome_summary = (
            f"Command finished with exit_code={result.exit_code}."
            if result.exit_code != 124
            else "Command timed out."
        )

        state.evidence.add_command_execution(
            command=result.command,
            producer=self.name,
            cwd=str(command_cwd),
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=(result.exit_code == 124),
            verification_goal=plan.verification_goal,
            rationale=plan.rationale,
            validation_claims=plan.validation_claims,
            expected_exit_codes=plan.expected_exit_codes,
            observed_outcome_summary=observed_outcome_summary,
        )

        state.evidence.add_note(
            message=(
                f"Command planned and executed from '{plan.cwd_relative_path}': "
                f"{plan.command} (exit_code={result.exit_code})"
            ),
            producer=self.name,
        )

        return state
