from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, model_validator
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

    @model_validator(mode="after")
    def validate_shape(self) -> "CommandVerificationPlan":
        self.command = self.command.strip()
        self.cwd_relative_path = (self.cwd_relative_path or ".").strip() or "."
        self.verification_goal = self.verification_goal.strip()
        self.rationale = self.rationale.strip()

        if not self.command:
            raise ValueError("command must not be empty.")

        if not self.verification_goal:
            raise ValueError("verification_goal must not be empty.")

        if not self.rationale:
            raise ValueError("rationale must not be empty.")

        if not self.expected_exit_codes:
            raise ValueError("expected_exit_codes must not be empty.")

        normalized_codes: list[int] = []
        seen_codes: set[int] = set()
        for code in self.expected_exit_codes:
            if not isinstance(code, int):
                raise ValueError("expected_exit_codes must contain integers only.")
            if code < 0:
                raise ValueError("expected_exit_codes must contain non-negative integers only.")
            if code not in seen_codes:
                seen_codes.add(code)
                normalized_codes.append(code)

        self.expected_exit_codes = normalized_codes
        self.validation_claims = [
            claim.strip()
            for claim in self.validation_claims
            if isinstance(claim, str) and claim.strip()
        ]

        return self


def _build_run_tree_inventory(run_dir: Path, *, max_files: int = 500) -> list[str]:
    return list_workspace_files(str(run_dir), max_files=max_files)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _contains_disallowed_shell_constructs(command: str) -> bool:
    disallowed_tokens = [
        "&&",
        "||",
        "|",
        ">",
        ">>",
        "<",
        ";",
    ]
    return any(token in command for token in disallowed_tokens)


def _build_command_planning_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
) -> str:
    inventory = _build_run_tree_inventory(run_dir)
    inventory_text = "\n".join(f"- {path}" for path in inventory) if inventory else "[empty]"

    changed_files = [item.model_dump(mode="json") for item in state.evidence.changed_files]
    files_read = [item.model_dump(mode="json") for item in state.evidence.files_read]
    commands = [item.model_dump(mode="json") for item in state.evidence.commands]
    notes = [item.message for item in state.evidence.notes if item.message]

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
- notes: {notes}
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


def _validate_planned_command(plan: CommandVerificationPlan) -> None:
    if _contains_disallowed_shell_constructs(plan.command):
        raise SubagentRejectedStepError(
            "Planned command contains disallowed shell constructs such as chaining, pipes, or redirection."
        )


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
            plan = CommandVerificationPlan.model_validate(raw)
        except ValidationError as exc:
            raise SubagentRejectedStepError(
                f"Invalid command verification plan output: {str(exc)}"
            ) from exc

        _validate_planned_command(plan)
        return plan

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

        overlay_paths = (
            _dedupe_preserve_order(
                [item.path for item in state.evidence.changed_files if item.path]
            )
            or None
        )

        run_dir: Path | None = None
        command_cwd: Path | None = None
        plan: CommandVerificationPlan | None = None

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

        timed_out = result.exit_code == 124
        exit_code_matched_expectation = result.exit_code in plan.expected_exit_codes

        observed_outcome_summary = (
            "Command timed out."
            if timed_out
            else (
                f"Command finished with exit_code={result.exit_code}, "
                f"which matched expected_exit_codes={plan.expected_exit_codes}."
                if exit_code_matched_expectation
                else (
                    f"Command finished with exit_code={result.exit_code}, "
                    f"which did not match expected_exit_codes={plan.expected_exit_codes}."
                )
            )
        )

        state.evidence.add_command_execution(
            command=result.command,
            producer=self.name,
            cwd=plan.cwd_relative_path,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=timed_out,
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

        if not exit_code_matched_expectation:
            state.add_risk_flags([f"command_exit_code_unexpected:{result.exit_code}"])
            state.evidence.add_note(
                message=(
                    f"Observed exit_code={result.exit_code} did not match "
                    f"expected_exit_codes={plan.expected_exit_codes} for command '{plan.command}'."
                ),
                producer=self.name,
            )

        return state
