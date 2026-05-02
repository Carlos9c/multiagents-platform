from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import ExecutionRequest
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.command_tool import CommandToolError, run_command
from app.execution_engine.tools.file_reader_tool import read_text_file
from app.execution_engine.tools.workspace_scan_tool import list_workspace_files
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import ProjectStorageService
from app.services.workspace_runtime import WorkspaceRuntimeError

logger = logging.getLogger(__name__)

COMMAND_FILE_SELECTION_SYSTEM_PROMPT = """
You are a repository-local verification context selector.

Your job is to inspect the candidate run-tree inventory for ONE already-atomic task and select
the smallest set of repository-relative files that should be read before deciding whether an
executable repository-local verification command is appropriate.

Return ONLY JSON matching the provided schema.

Hard rules:
- Select only files that appear in the provided candidate run-tree inventory.
- Select only repository-relative file paths.
- Do not select directories.
- Do not select duplicate paths.
- Select the smallest useful file set.
- Do not guess hidden files or tools that are not present in the inventory.
- Prefer files that clarify:
  - test layout
  - executable entrypoints
  - repository-local verification conventions
  - build/test configuration
  - changed implementation files relevant to the task
- If very little file inspection is needed, return only a few files.
- If executable verification is likely not applicable, you may still select zero or very few files.
""".strip()

COMMAND_RUNNER_AGENT_SYSTEM_PROMPT = """
You are a repository-local verification planner.

Your job is to inspect the candidate run tree for ONE already-atomic task and decide between exactly one of these two outcomes:
1. run_command
   - choose the single concrete command that should be executed
   - choose the working directory inside the candidate run tree
   - explain what that command is meant to verify for later external validation

2. verification_not_applicable
   - choose this when no meaningful repository-local executable verification would materially improve the evidence for the current task

Return ONLY JSON matching the provided schema.

Hard rules:
- Do not force a command when no meaningful repository-local verification exists.
- Repository-local verification is not automatically required just because files changed.
- Documentation, requirements, specification, README, and design-note tasks often do not need executable verification unless the task explicitly asks for it.
- If you choose run_command, choose exactly one concrete command.
- The command must be repo-local and narrow in purpose.
- Do not use shell chaining, pipes, redirection, or multiple commands.
- Prefer project-standard commands already supported by the repository layout.
- Use the smallest useful verification command.
- The working directory must be "." or a relative path inside the candidate run tree.
- Do not invent tools, executables, frameworks, entrypoints, or files that are not grounded in the provided inventory/context.
- The goal is to produce operational evidence for external validation, not to perform open-ended exploration.

Planning policy:
- Base your command decision on the actual candidate run-tree inventory and the inspected file contents.
- Prefer commands supported by the files and configuration actually present in the run tree.
- If the executable verification path is ambiguous, choose verification_not_applicable rather than guessing.
""".strip()


class CommandInspectionPlan(BaseModel):
    selected_paths: list[str] = Field(default_factory=list)
    selection_rationale: str
    verification_hypothesis: str

    @model_validator(mode="after")
    def validate_shape(self) -> "CommandInspectionPlan":
        normalized_paths: list[str] = []
        seen: set[str] = set()

        for path in self.selected_paths:
            if not isinstance(path, str):
                raise ValueError("selected_paths must contain strings only.")
            normalized = path.strip()
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            normalized_paths.append(normalized)

        self.selected_paths = normalized_paths
        self.selection_rationale = self.selection_rationale.strip()
        self.verification_hypothesis = self.verification_hypothesis.strip()

        if not self.selection_rationale:
            raise ValueError("selection_rationale must not be empty.")

        if not self.verification_hypothesis:
            raise ValueError("verification_hypothesis must not be empty.")

        if len(self.selected_paths) > 12:
            raise ValueError("selected_paths cannot contain more than 12 files.")

        return self


class CommandVerificationPlan(BaseModel):
    decision: Literal["run_command", "verification_not_applicable"]
    command: str = ""
    cwd_relative_path: str = "."
    verification_goal: str
    rationale: str
    validation_claims: list[str] = Field(default_factory=list)
    expected_exit_codes: list[int] = Field(default_factory=lambda: [0])

    @model_validator(mode="after")
    def validate_shape(self) -> "CommandVerificationPlan":
        self.command = (self.command or "").strip()
        self.cwd_relative_path = (self.cwd_relative_path or ".").strip() or "."
        self.verification_goal = self.verification_goal.strip()
        self.rationale = self.rationale.strip()

        if not self.verification_goal:
            raise ValueError("verification_goal must not be empty.")

        if not self.rationale:
            raise ValueError("rationale must not be empty.")

        if self.decision == "run_command":
            if not self.command:
                raise ValueError("command must not be empty when decision=run_command.")

            if not self.expected_exit_codes:
                raise ValueError("expected_exit_codes must not be empty when decision=run_command.")

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
        else:
            self.command = ""
            self.cwd_relative_path = "."
            self.expected_exit_codes = []
            self.validation_claims = []

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


def _build_file_selection_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
    inventory: list[str],
) -> str:
    inventory_text = "\n".join(f"- {path}" for path in inventory) if inventory else "[empty]"

    changed_files = [item.model_dump(mode="json") for item in state.evidence.changed_files]
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
- executor_type: {request.executor_type}

Command-step context:
- step_id: {step.id}
- orchestrator_rationale: {step.instructions}
- target_paths: {step.target_paths}

Candidate run tree root:
- absolute_path: {str(run_dir)}

Candidate run tree inventory:
{inventory_text}

Accumulated execution evidence so far:
- changed_files: {changed_files}
- prior_commands: {commands}
- notes: {notes}
- risk_flags: {state.risk_flags}
- step_notes: {state.step_notes}
- relevant_files: {request.context.relevant_files}

Selection instructions:
- Select the smallest set of files that should be read before deciding whether executable repository-local verification is applicable.
- Prefer files that clarify how verification should work in this repository.
- Prefer changed implementation files, test files, entrypoints, and build/test configuration files when relevant.
- Do not select files just because they exist.
- If executable verification likely does not apply, you may select zero files or only a minimal set.
""".strip()


def _filter_valid_selected_paths(
    *,
    selected_paths: list[str],
    inventory: list[str],
) -> list[str]:
    inventory_set = set(inventory)
    valid: list[str] = []

    for path in selected_paths:
        if path in inventory_set:
            valid.append(path)
        else:
            logger.warning(
                "command_runner_agent_selected_path_not_in_inventory skipping path=%s",
                path,
            )

    return valid


def _read_selected_files(
    *,
    run_dir: Path,
    selected_paths: list[str],
) -> list[dict]:
    run_root = run_dir.resolve()
    results: list[dict] = []

    for relative_path in selected_paths:
        candidate = (run_root / relative_path).resolve()

        try:
            candidate.relative_to(run_root)
        except ValueError as exc:
            raise SubagentRejectedStepError(
                f"Selected inspection file escapes the candidate run tree: {relative_path}"
            ) from exc

        if not candidate.exists():
            results.append(
                {
                    "path": relative_path,
                    "status": "missing",
                    "content": None,
                }
            )
            continue

        if not candidate.is_file():
            results.append(
                {
                    "path": relative_path,
                    "status": "not_a_file",
                    "content": None,
                }
            )
            continue

        try:
            content = read_text_file(str(candidate))
            results.append(
                {
                    "path": relative_path,
                    "status": "ok",
                    "content": content,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "path": relative_path,
                    "status": f"read_error:{str(exc)}",
                    "content": None,
                }
            )

    return results


def _build_command_planning_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
    inventory: list[str],
    inspection_plan: CommandInspectionPlan,
    inspected_files: list[dict],
) -> str:
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
- executor_type: {request.executor_type}

Command-step context:
- step_id: {step.id}
- orchestrator_rationale: {step.instructions}
- target_paths: {step.target_paths}

Candidate run tree root:
- absolute_path: {str(run_dir)}

Candidate run tree inventory:
{inventory_text}

Inspection plan used before command planning:
- selected_paths: {inspection_plan.selected_paths}
- selection_rationale: {inspection_plan.selection_rationale}
- verification_hypothesis: {inspection_plan.verification_hypothesis}

Inspected file contents:
{inspected_files}

Accumulated execution evidence so far:
- changed_files: {changed_files}
- files_read: {files_read}
- prior_commands: {commands}
- notes: {notes}
- risk_flags: {state.risk_flags}
- step_notes: {state.step_notes}

Planning instructions:
- First decide whether repository-local executable verification is meaningfully applicable now.
- If yes, return decision=run_command and choose the single most useful repository-local verification command to run now.
- If no, return decision=verification_not_applicable and explain why executable verification would not materially improve the evidence.
- Do not force a command for documentation/specification/requirements work unless the task explicitly calls for executable repository-local verification.
- If you choose a command, it must help external validation verify the task without re-running commands later.
- Choose the working directory relative to the candidate run tree.
- Ground the decision strictly in the provided run-tree inventory, inspected file contents, task context, and accumulated evidence.
- Prefer the smallest useful verification command.
- Prefer repository-supported executable paths over generic guesses.
""".strip()


def _build_file_selection_retry_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
    inventory: list[str],
    validation_error: str,
) -> str:
    base = _build_file_selection_prompt(
        request=request, step=step, state=state, run_dir=run_dir, inventory=inventory
    )
    return f"""Your previous output was invalid.

Validation error:
{validation_error}

You must correct your output and return valid JSON matching the schema.

Key corrections:
- selected_paths must only contain paths present in the candidate run-tree inventory
- Do not include paths that are not listed in the inventory
- selection_rationale and verification_hypothesis must not be empty

{base}""".strip()


def _build_command_planning_retry_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
    inventory: list[str],
    inspection_plan: "CommandInspectionPlan",
    inspected_files: list[dict],
    validation_error: str,
) -> str:
    base = _build_command_planning_prompt(
        request=request,
        step=step,
        state=state,
        run_dir=run_dir,
        inventory=inventory,
        inspection_plan=inspection_plan,
        inspected_files=inspected_files,
    )
    return f"""Your previous output was invalid.

Validation error:
{validation_error}

You must correct your output and return valid JSON matching the schema.

Key corrections:
- decision must be exactly "run_command" or "verification_not_applicable"
- If decision is "run_command": command and expected_exit_codes must not be empty
- If decision is "verification_not_applicable": command must be "" and expected_exit_codes must be []
- verification_goal and rationale must not be empty
- Do not use shell chaining, pipes, or redirection in the command

{base}""".strip()


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
    if plan.decision != "run_command":
        return

    if _contains_disallowed_shell_constructs(plan.command):
        raise SubagentRejectedStepError(
            "Planned command contains disallowed shell constructs such as chaining, pipes, or redirection."
        )


class CommandRunnerAgent(BaseSubagent):
    name = "command_runner_agent"

    def __init__(self, runtime: BaseAgentRuntime) -> None:
        self.runtime = runtime
        self.workspace_runtime = LocalWorkspaceRuntime(storage_service=ProjectStorageService())

    def _select_files_for_inspection(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
        run_dir: Path,
        inventory: list[str],
    ) -> CommandInspectionPlan:
        schema = to_openai_strict_json_schema(CommandInspectionPlan.model_json_schema())
        user_prompt = _build_file_selection_prompt(
            request=request, step=step, state=state, run_dir=run_dir, inventory=inventory
        )
        raw = self.runtime.generate_structured(
            system_prompt=COMMAND_FILE_SELECTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="execution_engine_command_file_selection",
            json_schema=schema,
        )

        try:
            plan = CommandInspectionPlan.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "command_runner_agent_inspection_plan_invalid_retrying task_id=%s error=%s",
                request.task_id,
                str(exc),
            )
            retry_raw = self.runtime.generate_structured(
                system_prompt=COMMAND_FILE_SELECTION_SYSTEM_PROMPT,
                user_prompt=_build_file_selection_retry_prompt(
                    request=request,
                    step=step,
                    state=state,
                    run_dir=run_dir,
                    inventory=inventory,
                    validation_error=str(exc),
                ),
                schema_name="execution_engine_command_file_selection",
                json_schema=schema,
            )
            try:
                plan = CommandInspectionPlan.model_validate(retry_raw)
            except ValidationError:
                logger.warning(
                    "command_runner_agent_inspection_plan_invalid_after_retry_using_fallback task_id=%s",
                    request.task_id,
                )
                plan = CommandInspectionPlan(
                    selected_paths=[],
                    selection_rationale=(
                        "File inspection plan could not be generated after retry; "
                        "proceeding without pre-inspected files."
                    ),
                    verification_hypothesis=(
                        "Command planning will proceed using only the inventory and accumulated evidence."
                    ),
                )

        plan.selected_paths = _filter_valid_selected_paths(
            selected_paths=plan.selected_paths,
            inventory=inventory,
        )
        return plan

    def _plan_command(
        self,
        *,
        request: ExecutionRequest,
        step: ExecutionStep,
        state: ResolutionState,
        run_dir: Path,
        inventory: list[str],
        inspection_plan: CommandInspectionPlan,
        inspected_files: list[dict],
    ) -> CommandVerificationPlan:
        schema = to_openai_strict_json_schema(CommandVerificationPlan.model_json_schema())
        user_prompt = _build_command_planning_prompt(
            request=request,
            step=step,
            state=state,
            run_dir=run_dir,
            inventory=inventory,
            inspection_plan=inspection_plan,
            inspected_files=inspected_files,
        )
        raw = self.runtime.generate_structured(
            system_prompt=COMMAND_RUNNER_AGENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="execution_engine_command_verification_plan",
            json_schema=schema,
        )

        try:
            plan = CommandVerificationPlan.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "command_runner_agent_command_plan_invalid_retrying task_id=%s error=%s",
                request.task_id,
                str(exc),
            )
            retry_raw = self.runtime.generate_structured(
                system_prompt=COMMAND_RUNNER_AGENT_SYSTEM_PROMPT,
                user_prompt=_build_command_planning_retry_prompt(
                    request=request,
                    step=step,
                    state=state,
                    run_dir=run_dir,
                    inventory=inventory,
                    inspection_plan=inspection_plan,
                    inspected_files=inspected_files,
                    validation_error=str(exc),
                ),
                schema_name="execution_engine_command_verification_plan",
                json_schema=schema,
            )
            try:
                plan = CommandVerificationPlan.model_validate(retry_raw)
            except ValidationError as retry_exc:
                raise SubagentRejectedStepError(
                    f"Invalid command verification plan after retry: {str(retry_exc)}"
                ) from retry_exc

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
        del db

        if step.subagent_name != self.name:
            raise SubagentRejectedStepError(
                f"{self.name} received a step for subagent '{step.subagent_name}'."
            )

        logger.info(
            "command_runner_agent_started task_id=%s step_id=%s",
            request.task_id,
            step.id,
        )

        overlay_paths = (
            _dedupe_preserve_order(
                [item.path for item in state.evidence.changed_files if item.path]
            )
            or None
        )

        run_dir: Path | None = None
        inventory: list[str] = []
        inspection_plan: CommandInspectionPlan | None = None
        inspected_files: list[dict] = []
        plan: CommandVerificationPlan | None = None

        try:
            run_dir = self.workspace_runtime.materialize_run_tree(
                project_id=request.project_id,
                execution_run_id=request.execution_run_id,
                overlay_paths=overlay_paths,
            )

            logger.info(
                "command_runner_agent_run_tree_materialized task_id=%s run_dir=%s",
                request.task_id,
                str(run_dir),
            )

            inventory = _build_run_tree_inventory(run_dir)

            inspection_plan = self._select_files_for_inspection(
                request=request,
                step=step,
                state=state,
                run_dir=run_dir,
                inventory=inventory,
            )

            inspected_files = _read_selected_files(
                run_dir=run_dir,
                selected_paths=inspection_plan.selected_paths,
            )

            for item in inspected_files:
                if item["status"] == "ok":
                    state.evidence.add_file_read(
                        path=item["path"],
                        producer=self.name,
                        source="run_tree_inspection",
                    )

            plan = self._plan_command(
                request=request,
                step=step,
                state=state,
                run_dir=run_dir,
                inventory=inventory,
                inspection_plan=inspection_plan,
                inspected_files=inspected_files,
            )

            if plan.decision == "verification_not_applicable":
                logger.info(
                    "command_runner_agent_verification_skipped task_id=%s rationale=%s",
                    request.task_id,
                    plan.rationale,
                )
                state.evidence.add_note(
                    message=(
                        "Command verification was evaluated and deemed not materially applicable "
                        f"for this task: {plan.rationale}"
                    ),
                    producer=self.name,
                )
                state.evidence.add_note(
                    message=f"Verification goal assessment: {plan.verification_goal}",
                    producer=self.name,
                )
                return state

            logger.info(
                "command_runner_agent_executing task_id=%s command=%s cwd=%s expected_exit_codes=%s",
                request.task_id,
                plan.command,
                plan.cwd_relative_path,
                plan.expected_exit_codes,
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
                logger.warning(
                    "command_runner_agent_run_tree_cleanup_failed task_id=%s run_id=%s",
                    request.task_id,
                    request.execution_run_id,
                    exc_info=True,
                )

        timed_out = result.exit_code == 124
        exit_code_matched_expectation = result.exit_code in plan.expected_exit_codes

        logger.info(
            "command_runner_agent_command_result task_id=%s exit_code=%s timed_out=%s matched=%s",
            request.task_id,
            result.exit_code,
            timed_out,
            exit_code_matched_expectation,
        )

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

        if inspection_plan is not None:
            state.evidence.add_note(
                message=(
                    f"Selected {len(inspection_plan.selected_paths)} run-tree files for command "
                    f"planning before choosing verification: {inspection_plan.selected_paths}"
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
