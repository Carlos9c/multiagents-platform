from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.contracts import OBSERVATION_TYPE_ERROR_DIAGNOSIS, ExecutionRequest
from app.execution_engine.error_diagnosis import ErrorDiagnosis
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import BaseSubagent, SubagentRejectedStepError
from app.execution_engine.tools.command_tool import CommandToolError, run_command
from app.execution_engine.tools.error_diagnostic_tool import run_error_diagnosis
from app.execution_engine.tools.file_reader_tool import read_text_file
from app.execution_engine.tools.workspace_scan_tool import list_workspace_files
from app.services.environment.registry import get_driver_registry
from app.services.environment.session_store import get_session_store
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import ProjectStorageService
from app.services.prompt_loader import prompt_loader
from app.services.workspace_runtime import WorkspaceRuntimeError

logger = logging.getLogger(__name__)

COMMAND_FILE_SELECTION_SYSTEM_PROMPT = prompt_loader.get("command_runner_agent", "file_selection")

COMMAND_RUNNER_AGENT_SYSTEM_PROMPT = prompt_loader.get("command_runner_agent")


def _format_runtime_spec_for_prompt(runtime_spec: dict | None) -> str:
    if not runtime_spec:
        return "[no runtime environment spec — commands run locally]"
    runtime_type = runtime_spec.get("runtime_type", "unknown")
    image = runtime_spec.get("image", "unknown")
    deps = runtime_spec.get("dependencies", [])
    dep_lines = [
        f"  - {d.get('name', '?')}=={d.get('version', '?')}"
        + (f"[{','.join(d['extras'])}]" if d.get("extras") else "")
        for d in deps
    ]
    lines = [
        f"runtime_type: {runtime_type} (commands run inside Docker container)",
        f"image: {image}",
        "installed_packages:" if dep_lines else "installed_packages: []",
    ]
    lines.extend(dep_lines)
    return "\n".join(lines)


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
    cwd_relative_path: str = Field(
        default=".",
        description=(
            "Working directory for the command, as a path relative to the run-tree root. "
            "Determine the correct value by examining the run-tree inventory and inspected "
            "file contents: look for build configuration files, test runner configs, "
            "Makefiles, package manifests, or executable entrypoints that indicate where "
            "the command should run. '.' (the run-tree root) is the default and is correct "
            "for most projects. Only use a subdirectory when you find concrete evidence in "
            "the inventory that the command must run from that specific location."
        ),
    )
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


_DISALLOWED_SHELL_OPERATORS = frozenset({"&&", "||", "|", ">", ">>", "<", ";"})


def _contains_disallowed_shell_constructs(command: str) -> bool:
    """Return True if *command* contains shell operators at the top level.

    Uses shlex.split() so that operators inside quoted arguments are ignored.
    Example: ``python -c "import sys; print(sys.version)"`` is safe (the ``;``
    lives inside a quoted string token) while ``pytest && echo done`` is not.

    An unparseable command (unmatched quotes, etc.) is treated as unsafe.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unbalanced quotes or other parse error → treat as unsafe
        return True
    return any(token in _DISALLOWED_SHELL_OPERATORS for token in tokens)


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

    runtime_spec_context = _format_runtime_spec_for_prompt(request.context.runtime_spec)

    prompt_loader.validate_builder_inputs(
        "command_runner_agent",
        "file_selection",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "objective": request.objective,
            "acceptance_criteria": request.acceptance_criteria,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "tests_required": request.tests_required,
            "executor_type": request.executor_type,
            "runtime_spec_context": runtime_spec_context,
            "step_id": step.id,
            "step_instructions": step.instructions,
            "step_target_paths": step.target_paths,
            "run_dir_path": str(run_dir),
            "run_tree_inventory": inventory_text,
            "changed_files": changed_files,
            "prior_commands": commands,
            "evidence_notes": notes,
            "risk_flags": state.risk_flags,
            "step_notes": state.step_notes,
            "relevant_files": request.context.relevant_files,
        },
    )
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

Runtime environment:
{runtime_spec_context}

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
    runtime_spec_context = _format_runtime_spec_for_prompt(request.context.runtime_spec)

    prompt_loader.validate_builder_inputs(
        "command_runner_agent",
        "main",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "objective": request.objective,
            "acceptance_criteria": request.acceptance_criteria,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "tests_required": request.tests_required,
            "executor_type": request.executor_type,
            "runtime_spec_context": runtime_spec_context,
            "step_id": step.id,
            "step_instructions": step.instructions,
            "step_target_paths": step.target_paths,
            "run_dir_path": str(run_dir),
            "run_tree_inventory": inventory_text,
            "inspection_selected_paths": inspection_plan.selected_paths,
            "inspection_selection_rationale": inspection_plan.selection_rationale,
            "inspection_verification_hypothesis": inspection_plan.verification_hypothesis,
            "inspected_files": inspected_files,
            "changed_files": changed_files,
            "files_read": files_read,
            "prior_commands": commands,
            "evidence_notes": notes,
            "risk_flags": state.risk_flags,
            "step_notes": state.step_notes,
        },
    )
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

Runtime environment:
{runtime_spec_context}

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
- If the inventory contains test or spec files under a subdirectory (e.g., tests/, spec/, __tests__/), you MUST pass that subdirectory explicitly to the test runner. Bare runner invocations without path arguments (e.g., "python -m unittest" with no args) discover zero tests when the test directory is not the project root, which makes the command useless. Always construct the command so it will find the tests you can see in the inventory.
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
    inventory_text = "\n".join(f"- {path}" for path in inventory) if inventory else "[empty]"
    changed_files = [item.model_dump(mode="json") for item in state.evidence.changed_files]
    commands = [item.model_dump(mode="json") for item in state.evidence.commands]
    notes = [item.message for item in state.evidence.notes if item.message]
    runtime_spec_context = _format_runtime_spec_for_prompt(request.context.runtime_spec)
    prompt_loader.validate_builder_inputs(
        "command_runner_agent",
        "file_selection_retry",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "objective": request.objective,
            "acceptance_criteria": request.acceptance_criteria,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "tests_required": request.tests_required,
            "executor_type": request.executor_type,
            "runtime_spec_context": runtime_spec_context,
            "step_id": step.id,
            "step_instructions": step.instructions,
            "step_target_paths": step.target_paths,
            "run_dir_path": str(run_dir),
            "run_tree_inventory": inventory_text,
            "changed_files": changed_files,
            "prior_commands": commands,
            "evidence_notes": notes,
            "risk_flags": state.risk_flags,
            "step_notes": state.step_notes,
            "relevant_files": request.context.relevant_files,
            "validation_error": validation_error,
        },
    )
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
    inventory_text = "\n".join(f"- {path}" for path in inventory) if inventory else "[empty]"
    changed_files = [item.model_dump(mode="json") for item in state.evidence.changed_files]
    files_read = [item.model_dump(mode="json") for item in state.evidence.files_read]
    commands = [item.model_dump(mode="json") for item in state.evidence.commands]
    notes = [item.message for item in state.evidence.notes if item.message]
    runtime_spec_context = _format_runtime_spec_for_prompt(request.context.runtime_spec)
    prompt_loader.validate_builder_inputs(
        "command_runner_agent",
        "main_retry",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "objective": request.objective,
            "acceptance_criteria": request.acceptance_criteria,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "tests_required": request.tests_required,
            "executor_type": request.executor_type,
            "runtime_spec_context": runtime_spec_context,
            "step_id": step.id,
            "step_instructions": step.instructions,
            "step_target_paths": step.target_paths,
            "run_dir_path": str(run_dir),
            "run_tree_inventory": inventory_text,
            "inspection_selected_paths": inspection_plan.selected_paths,
            "inspection_selection_rationale": inspection_plan.selection_rationale,
            "inspection_verification_hypothesis": inspection_plan.verification_hypothesis,
            "inspected_files": inspected_files,
            "changed_files": changed_files,
            "files_read": files_read,
            "prior_commands": commands,
            "evidence_notes": notes,
            "risk_flags": state.risk_flags,
            "step_notes": state.step_notes,
            "validation_error": validation_error,
        },
    )
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


def _build_command_planning_constraint_retry_prompt(
    *,
    request: ExecutionRequest,
    step: ExecutionStep,
    state: ResolutionState,
    run_dir: Path,
    inventory: list[str],
    inspection_plan: "CommandInspectionPlan",
    inspected_files: list[dict],
    constraint_error: str,
) -> str:
    inventory_text = "\n".join(f"- {path}" for path in inventory) if inventory else "[empty]"
    changed_files = [item.model_dump(mode="json") for item in state.evidence.changed_files]
    files_read = [item.model_dump(mode="json") for item in state.evidence.files_read]
    commands = [item.model_dump(mode="json") for item in state.evidence.commands]
    notes = [item.message for item in state.evidence.notes if item.message]
    runtime_spec_context = _format_runtime_spec_for_prompt(request.context.runtime_spec)
    prompt_loader.validate_builder_inputs(
        "command_runner_agent",
        "main_constraint_retry",
        {
            "task_id": request.task_id,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "objective": request.objective,
            "acceptance_criteria": request.acceptance_criteria,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "tests_required": request.tests_required,
            "executor_type": request.executor_type,
            "runtime_spec_context": runtime_spec_context,
            "step_id": step.id,
            "step_instructions": step.instructions,
            "step_target_paths": step.target_paths,
            "run_dir_path": str(run_dir),
            "run_tree_inventory": inventory_text,
            "inspection_selected_paths": inspection_plan.selected_paths,
            "inspection_selection_rationale": inspection_plan.selection_rationale,
            "inspection_verification_hypothesis": inspection_plan.verification_hypothesis,
            "inspected_files": inspected_files,
            "changed_files": changed_files,
            "files_read": files_read,
            "prior_commands": commands,
            "evidence_notes": notes,
            "risk_flags": state.risk_flags,
            "step_notes": state.step_notes,
            "constraint_error": constraint_error,
        },
    )
    base = _build_command_planning_prompt(
        request=request,
        step=step,
        state=state,
        run_dir=run_dir,
        inventory=inventory,
        inspection_plan=inspection_plan,
        inspected_files=inspected_files,
    )
    return (
        f"""Your previous command plan was rejected because it violated a hard execution constraint.

Constraint violation:
{constraint_error}

You must plan a different command that does NOT use shell constructs such as &&, ||, |, >, >>, <, or ;.

Correction rules:
- Choose exactly ONE self-contained command. No chaining, no pipes, no redirection.
- If the verification you had in mind required multiple steps, pick only the single most valuable one.
- If no single allowed command can provide meaningful verification, choose verification_not_applicable.
- Prefer narrow commands that are already supported by the repository layout (e.g. a test runner, a compiler check, a lint tool invocation).

{base}""".strip()
    )


def _normalize_android_shell_line_endings(run_dir: Path) -> None:
    """Strip CR from shell scripts in the run tree (android_gradle context only).

    LLM-generated shell scripts may carry \\r\\n line endings from Windows. Inside
    a Linux container `bash` treats the trailing \\r as part of the option name
    (e.g. `set -e\\r`) and exits with "invalid option name". Normalizing on the
    host before Docker reads the files avoids this without touching non-Android runs.
    """
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "gradlew" or path.suffix == ".sh":
            try:
                raw = path.read_bytes()
                if b"\r\n" in raw:
                    path.write_bytes(raw.replace(b"\r\n", b"\n"))
            except Exception:
                logger.warning(
                    "command_runner_agent_crlf_normalize_failed path=%s",
                    path,
                    exc_info=True,
                )


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


def _read_files_for_context_expansion(
    *,
    paths: list[str],
    workspace_root: str,
    source_root: str,
) -> dict[str, str]:
    """
    Read repository files from the workspace overlay or source baseline.

    Called after the ephemeral run tree is cleaned up to preload the files listed
    in ErrorDiagnosis.newly_discovered_files into the execution context so the next
    subagent (e.g. code_change_agent) has immediate access to them.

    Workspace overlay takes priority over source baseline (same lookup order as the
    file-writing subagents). Files that cannot be found in either layer are skipped
    silently.
    """
    workspace = Path(workspace_root).resolve()
    source = Path(source_root).resolve()
    contents: dict[str, str] = {}

    for rel_path in paths:
        if not rel_path or not rel_path.strip():
            continue
        clean = rel_path.strip()
        if clean in contents:
            continue
        for root in (workspace, source):
            candidate = (root / clean).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                try:
                    contents[clean] = candidate.read_text(encoding="utf-8")
                except Exception:
                    pass
                break

    return contents


def _try_run_error_diagnosis(
    *,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    run_dir: Path,
    request: ExecutionRequest,
    runtime: BaseAgentRuntime,
    changed_files: list | None = None,
) -> ErrorDiagnosis | None:
    """
    Attempt error diagnosis while the run tree is still available.

    changed_files: the current evidence.changed_files list, forwarded to the
      diagnostic tool so the LLM can determine fault_side (implementation vs. test
      vs. environment) using recent repository changes as context.

    Any exception raised by the tool is caught and logged so that a diagnosis
    failure never prevents command_runner_agent from completing its own step.
    """
    try:
        return run_error_diagnosis(
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            run_dir=run_dir,
            request=request,
            runtime=runtime,
            changed_files=changed_files,
        )
    except Exception as exc:
        logger.warning(
            "command_runner_agent_error_diagnosis_failed command=%s error=%s",
            command,
            str(exc),
            exc_info=True,
        )
        return None


def _write_command_runner_trace(
    *,
    request: "ExecutionRequest",
    call_type: str,
    command: str = "",
    exit_code: int | None = None,
    success: bool | None = None,
    timed_out: bool = False,
    stdout_preview: str = "",
    stderr_preview: str = "",
    verification_goal: str = "",
    rationale: str = "",
) -> None:
    """Append a command_runner_agent trace entry to execution_trace.jsonl.

    The Supervisor reads these entries to assess command execution patterns
    since command evidence is not separately persisted after runs complete.
    """
    from app.services.supervisor.trace_writer import append_execution_trace

    commands: list[dict] = []
    if call_type == "command_executed":
        commands = [
            {
                "command": command,
                "exit_code": exit_code,
                "success": success,
                "timed_out": timed_out,
                "stdout_preview": stdout_preview[:200],
                "stderr_preview": stderr_preview[:200],
                "verification_goal": verification_goal,
            }
        ]

    entry: dict = {
        "agent": "command_runner_agent",
        "project_id": request.project_id,
        "task_id": request.task_id,
        "run_id": request.execution_run_id,
        "call_type": call_type,
        "rationale": rationale,
        "commands": commands,
    }
    append_execution_trace(project_id=request.project_id, entry=entry)


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
        user_prompt = _build_file_selection_prompt(
            request=request, step=step, state=state, run_dir=run_dir, inventory=inventory
        )
        raw = self.runtime.generate_structured(
            system_prompt=COMMAND_FILE_SELECTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="execution_engine_command_file_selection",
            json_schema=CommandInspectionPlan.model_json_schema(),
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
                json_schema=CommandInspectionPlan.model_json_schema(),
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
            json_schema=CommandVerificationPlan.model_json_schema(),
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
                json_schema=CommandVerificationPlan.model_json_schema(),
            )
            try:
                plan = CommandVerificationPlan.model_validate(retry_raw)
            except ValidationError as retry_exc:
                raise SubagentRejectedStepError(
                    f"Invalid command verification plan after retry: {str(retry_exc)}"
                ) from retry_exc

        try:
            _validate_planned_command(plan)
        except SubagentRejectedStepError as constraint_exc:
            logger.warning(
                "command_runner_agent_command_plan_constraint_violation_retrying task_id=%s error=%s",
                request.task_id,
                str(constraint_exc),
            )
            constraint_retry_raw = self.runtime.generate_structured(
                system_prompt=COMMAND_RUNNER_AGENT_SYSTEM_PROMPT,
                user_prompt=_build_command_planning_constraint_retry_prompt(
                    request=request,
                    step=step,
                    state=state,
                    run_dir=run_dir,
                    inventory=inventory,
                    inspection_plan=inspection_plan,
                    inspected_files=inspected_files,
                    constraint_error=str(constraint_exc),
                ),
                schema_name="execution_engine_command_verification_plan",
                json_schema=CommandVerificationPlan.model_json_schema(),
            )
            try:
                plan = CommandVerificationPlan.model_validate(constraint_retry_raw)
            except ValidationError as retry_exc:
                raise SubagentRejectedStepError(
                    f"Invalid command verification plan after constraint retry: {str(retry_exc)}"
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

        docker_session = get_session_store().get_session(request.project_id)

        run_dir: Path | None = None
        inventory: list[str] = []
        inspection_plan: CommandInspectionPlan | None = None
        inspected_files: list[dict] = []
        plan: CommandVerificationPlan | None = None
        diagnosis: ErrorDiagnosis | None = None

        try:
            run_dir = self.workspace_runtime.materialize_run_tree(
                project_id=request.project_id,
                execution_run_id=request.execution_run_id,
                overlay_paths=overlay_paths,
            )

            if docker_session is not None and docker_session.runtime_type == "android_gradle":
                _normalize_android_shell_line_endings(run_dir)

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

            try:
                plan = self._plan_command(
                    request=request,
                    step=step,
                    state=state,
                    run_dir=run_dir,
                    inventory=inventory,
                    inspection_plan=inspection_plan,
                    inspected_files=inspected_files,
                )
            except SubagentRejectedStepError as exc:
                state.evidence.add_note(
                    message=(
                        f"command_runner_agent: verification planning aborted — "
                        f"could not produce a valid executable command after retries: {exc}"
                    ),
                    producer=self.name,
                )
                raise

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
                _write_command_runner_trace(
                    request=request,
                    call_type="verification_not_applicable",
                    verification_goal=plan.verification_goal,
                    rationale=plan.rationale,
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

            if docker_session is not None:
                driver = get_driver_registry().get_driver(docker_session.runtime_type)
                docker_result = driver.run_command(docker_session, plan.command, command_cwd)
                result_command = plan.command
                result_exit_code = docker_result.exit_code
                result_stdout = docker_result.stdout
                result_stderr = docker_result.stderr
            else:
                local_result = run_command(command=plan.command, cwd=str(command_cwd))
                result_command = local_result.command
                result_exit_code = local_result.exit_code
                result_stdout = local_result.stdout
                result_stderr = local_result.stderr

            # Run error diagnosis while the run tree is still available (before finally cleanup).
            # Only triggered on unexpected exit codes — successes need no diagnosis.
            # Pass changed_files so the LLM can determine fault_side (implementation /
            # test / environment) using the recent repository changes as context.
            if run_dir is not None and result_exit_code not in plan.expected_exit_codes:
                diagnosis = _try_run_error_diagnosis(
                    command=result_command,
                    exit_code=result_exit_code,
                    stdout=result_stdout or "",
                    stderr=result_stderr or "",
                    run_dir=run_dir,
                    request=request,
                    runtime=self.runtime,
                    changed_files=list(state.evidence.changed_files),
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

        timed_out = result_exit_code == 124
        exit_code_matched_expectation = result_exit_code in plan.expected_exit_codes

        logger.info(
            "command_runner_agent_command_result task_id=%s exit_code=%s timed_out=%s matched=%s",
            request.task_id,
            result_exit_code,
            timed_out,
            exit_code_matched_expectation,
        )

        observed_outcome_summary = (
            "Command timed out."
            if timed_out
            else (
                f"Command finished with exit_code={result_exit_code}, "
                f"which matched expected_exit_codes={plan.expected_exit_codes}."
                if exit_code_matched_expectation
                else (
                    f"Command finished with exit_code={result_exit_code}, "
                    f"which did not match expected_exit_codes={plan.expected_exit_codes}."
                )
            )
        )

        state.evidence.add_command_execution(
            command=result_command,
            producer=self.name,
            cwd=plan.cwd_relative_path,
            exit_code=result_exit_code,
            stdout=result_stdout,
            stderr=result_stderr,
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
                f"{plan.command} (exit_code={result_exit_code})"
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
            state.add_risk_flags([f"command_exit_code_unexpected:{result_exit_code}"])
            state.evidence.add_note(
                message=(
                    f"Observed exit_code={result_exit_code} did not match "
                    f"expected_exit_codes={plan.expected_exit_codes} for command '{plan.command}'."
                ),
                producer=self.name,
            )

            if diagnosis is not None:
                state.evidence.add_observation(
                    evidence_type=OBSERVATION_TYPE_ERROR_DIAGNOSIS,
                    producer=self.name,
                    summary=diagnosis.repair_directive,
                    payload=diagnosis.model_dump(mode="json"),
                )

                if diagnosis.newly_discovered_files:
                    new_files = _read_files_for_context_expansion(
                        paths=diagnosis.newly_discovered_files,
                        workspace_root=request.context.workspace_path,
                        source_root=request.context.source_path,
                    )
                    if new_files:
                        expanded = {
                            **request.context.preloaded_dependency_files,
                            **new_files,
                        }
                        new_context = request.context.model_copy(
                            update={"preloaded_dependency_files": expanded}
                        )
                        new_request = request.model_copy(update={"context": new_context})
                        state.replace_execution_request(new_request)
                        state.evidence.add_note(
                            message=(
                                f"Error diagnosis expanded execution context with "
                                f"{len(new_files)} newly_discovered file(s): "
                                f"{list(new_files.keys())}"
                            ),
                            producer=self.name,
                        )

        _write_command_runner_trace(
            request=request,
            call_type="command_executed",
            command=result_command,
            exit_code=result_exit_code,
            success=exit_code_matched_expectation,
            timed_out=timed_out,
            stdout_preview=result_stdout or "",
            stderr_preview=result_stderr or "",
            verification_goal=plan.verification_goal,
            rationale=plan.rationale,
        )
        return state
