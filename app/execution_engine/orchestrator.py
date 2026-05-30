from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.base import ExecutionEngineRejectedError, ExecutionEngineTransientError
from app.execution_engine.budget import LoopBudget
from app.execution_engine.capabilities import render_executor_capabilities_for_prompt
from app.execution_engine.contracts import (
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_FAILED,
    OBSERVATION_TYPE_ERROR_DIAGNOSIS,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
)
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.monitoring import OrchestratorTrace
from app.execution_engine.next_action import (
    DECISION_CALL_SUBAGENT,
    DECISION_FINISH,
    DECISION_INVALID,
    DECISION_REJECT,
    NextActionDecision,
)
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.state import ExecutionState
from app.execution_engine.subagent_registry import (
    SubagentRegistry,
    SubagentRegistryError,
)
from app.execution_engine.subagents.base import SubagentRejectedStepError
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

ORCHESTRATOR_SYSTEM_PROMPT = prompt_loader.get("orchestrator", "decision")


def _is_transient_infrastructure_error(exc: BaseException) -> bool:
    """Return True if exc (or any cause in its chain) is an HTTP 5xx from OpenAI/Cloudflare."""
    try:
        from openai import InternalServerError as _OpenAIInternalServerError
    except ImportError:
        return False

    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, _OpenAIInternalServerError):
            status = getattr(current, "status_code", None)
            if status is not None and status >= 500:
                return True
        current = current.__cause__ or current.__context__
    return False


def _append_trace_notes_to_evidence(resolution_state: ResolutionState) -> None:
    if resolution_state.orchestrator_trace is None:
        return

    for note in resolution_state.orchestrator_trace.to_notes():
        resolution_state.evidence.add_note(
            message=note,
            producer="execution_orchestrator",
        )


def _render_files_read_for_prompt(resolution_state: ResolutionState) -> list[dict]:
    return [item.model_dump() for item in resolution_state.evidence.files_read]


def _render_notes_for_prompt(resolution_state: ResolutionState) -> list[dict]:
    return [item.model_dump() for item in resolution_state.evidence.notes]


def _render_commands_for_prompt(resolution_state: ResolutionState) -> list[dict]:
    return [item.model_dump() for item in resolution_state.evidence.commands]


def _render_artifacts_created_for_prompt(resolution_state: ResolutionState) -> list[dict]:
    return [item.model_dump() for item in resolution_state.evidence.artifacts_created]


def _render_change_dependencies_for_prompt(resolution_state: ResolutionState) -> list[dict]:
    return [item.model_dump() for item in resolution_state.evidence.change_dependencies]


def _render_evidence_items_for_prompt(resolution_state: ResolutionState) -> list[dict]:
    return [item.model_dump() for item in resolution_state.evidence.to_evidence_items()]


def _extract_subagent_name_from_step_id(step_id: str) -> str | None:
    prefix = "dynamic_call_"
    if not step_id.startswith(prefix):
        return None

    suffix = step_id[len(prefix) :]
    if not suffix:
        return None

    parts = suffix.split("_", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]

    return suffix


def _last_completed_subagent_name(resolution_state: ResolutionState) -> str | None:
    if not resolution_state.completed_steps:
        return None

    return _extract_subagent_name_from_step_id(resolution_state.completed_steps[-1])


def _last_attempted_subagent_name(runtime_state: ExecutionState) -> str | None:
    for agent_name in reversed(runtime_state.visited_agents):
        if agent_name in {
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
            "document_writer_agent",
            "test_builder_agent",
            "environment_manager_agent",
        }:
            return agent_name
    return None


# Task types that inherently require materializing file changes.
# "review" is the only planner-assigned type where no code output is expected.
# Recovery variants "test", "refactor", "configuration" are also included.
TASK_TYPES_REQUIRING_CHANGES = frozenset(
    {
        "implementation",
        "testing",
        "documentation",
        "onboarding",
        "design",
        "planning",
        "requirements",
        "refactor",
        "configuration",
    }
)

# Task types where writing tests is an explicit goal.
TASK_TYPES_REQUIRING_VERIFICATION = frozenset({"testing"})


def _allowed_subagents_for_phase(phase: str) -> list[str]:
    if phase == "discovery":
        return ["context_selection_agent"]

    if phase == "execution":
        return [
            "context_selection_agent",
            "code_change_agent",
            "command_runner_agent",
            "document_writer_agent",
            "test_builder_agent",
            "environment_manager_agent",
        ]

    return []


def _advance_phase_after_step(
    resolution_state: ResolutionState,
    *,
    decision: NextActionDecision,
) -> None:
    if (
        resolution_state.phase == "discovery"
        and decision.decision_type == DECISION_CALL_SUBAGENT
        and decision.subagent_name == "context_selection_agent"
    ):
        resolution_state.phase = "execution"


def _task_text(request: ExecutionRequest) -> str:
    parts = [
        request.task_title or "",
        request.task_description or "",
        request.objective or "",
        request.acceptance_criteria or "",
        request.technical_constraints or "",
        request.out_of_scope or "",
    ]
    return " \n".join(part for part in parts if part).lower()


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _is_documentation_like_task(request: ExecutionRequest) -> bool:
    text = _task_text(request)
    doc_markers = (
        "documentation",
        "document",
        "docs",
        "readme",
        "specification",
        "spec",
        "requirements",
        "requirement",
        "contract",
        "design note",
        "design notes",
        "arquitectura",
        "architecture",
        "especificación",
        "requisitos",
        "documentación",
        "contrato funcional",
    )
    return _contains_any(text, doc_markers)


def _is_executable_implementation_like_task(request: ExecutionRequest) -> bool:
    text = _task_text(request)
    implementation_markers = (
        "implement",
        "implementation",
        "fix",
        "bug",
        "change behavior",
        "modify behavior",
        "endpoint",
        "api",
        "function",
        "service",
        "handler",
        "controller",
        "repository",
        "schema",
        "model",
        "migration",
        "test",
        "refactor",
        "script",
        "cli",
        "command",
        "python",
        "create",
        "list",
    )
    return _contains_any(text, implementation_markers)


def _task_explicitly_requests_repo_local_verification(request: ExecutionRequest) -> bool:
    if request.verification_level == "none":
        return False

    if request.task_type and request.task_type.lower() in TASK_TYPES_REQUIRING_VERIFICATION:
        return True

    text = _task_text(request)
    verification_markers = (
        # English
        "test",
        "tests",
        "verify",
        "verification",
        "validated",
        "validation",
        "check",
        "assert",
        "run",
        "lint",
        "build",
        "compile",
        "smoke",
        "prove",
        "pytest",
        "unit test",
        "integration test",
        "repository-local verification",
        # Spanish
        "prueba",
        "pruebas",
        "verificar",
        "verificación",
        "validar",
        "validación",
        "comprobar",
        "ejecutar",
        "construir",
        "compilar",
    )
    return _contains_any(text, verification_markers)


def _task_requires_material_changes(request: ExecutionRequest) -> bool:
    if request.task_type and request.task_type.lower() in TASK_TYPES_REQUIRING_CHANGES:
        return True

    text = _task_text(request)
    change_markers = (
        # English
        "implement",
        "implementation",
        "fix",
        "bug",
        "change",
        "modify",
        "update",
        "edit",
        "refactor",
        "create",
        "add",
        "remove",
        "write",
        "documentation",
        "docs",
        "readme",
        "test",
        "migration",
        "endpoint",
        "schema",
        "model",
        "specification",
        "requirements",
        "document",
        # Spanish
        "implementar",
        "arreglar",
        "cambiar",
        "modificar",
        "actualizar",
        "crear",
        "agregar",
        "añadir",
        "eliminar",
        "escribir",
        "prueba",
        "pruebas",
        "migración",
        "esquema",
        "especificación",
        "requisitos",
        "documentación",
    )
    return _contains_any(text, change_markers)


def _code_change_agent_completed_a_step(resolution_state: ResolutionState) -> bool:
    return any(
        _extract_subagent_name_from_step_id(step_id) == "code_change_agent"
        for step_id in resolution_state.completed_steps
    )


def _document_writer_agent_completed_a_step(resolution_state: ResolutionState) -> bool:
    return any(
        _extract_subagent_name_from_step_id(step_id) == "document_writer_agent"
        for step_id in resolution_state.completed_steps
    )


def _test_builder_agent_completed_a_step(resolution_state: ResolutionState) -> bool:
    return any(
        _extract_subagent_name_from_step_id(step_id) == "test_builder_agent"
        for step_id in resolution_state.completed_steps
    )


def _changed_files_look_documentation_only(resolution_state: ResolutionState) -> bool:
    changed_files = resolution_state.evidence.changed_files
    if not changed_files:
        return False

    documentation_suffixes = (
        ".md",
        ".rst",
        ".txt",
        ".adoc",
    )
    documentation_path_markers = (
        "/docs/",
        "docs/",
        "readme",
        "spec",
        "requirements",
        "design",
    )

    for item in changed_files:
        path = item.path.lower()
        if path.endswith(documentation_suffixes):
            continue
        if any(marker in path for marker in documentation_path_markers):
            continue
        return False

    return True


def _latest_command_execution(resolution_state: ResolutionState):
    commands = resolution_state.evidence.commands
    if not commands:
        return None
    return commands[-1]


def _command_succeeded(command) -> bool:
    if command is None:
        return False

    if bool(getattr(command, "timed_out", False)):
        return False

    exit_code = getattr(command, "exit_code", None)
    expected_exit_codes = list(getattr(command, "expected_exit_codes", []) or [])
    if expected_exit_codes:
        return exit_code in expected_exit_codes

    return exit_code == 0


_SETUP_ONLY_COMMAND_PREFIXES: tuple[str, ...] = (
    "chmod ",
    "mkdir ",
    "cp ",
    "mv ",
    "ln ",
    "touch ",
    "export ",
    "apt ",
    "apt-get ",
    "pip install",
    "npm install",
    "yarn install",
    "bundle install",
    "brew install",
)


def _command_is_setup_only(command) -> bool:
    """Return True when command is infrastructure prep, not objective verification.

    Setup commands (chmod, mkdir, cp, etc.) enable subsequent work but do not
    verify the task objective. A successful setup command must not be treated as
    evidence that acceptance-criteria verification has been completed.
    """
    if command is None:
        return False
    cmd = (getattr(command, "command", "") or "").strip().lower()
    return any(cmd.startswith(prefix) for prefix in _SETUP_ONLY_COMMAND_PREFIXES)


def _verification_would_materially_improve(
    request: ExecutionRequest,
    resolution_state: ResolutionState,
) -> bool:
    if request.verification_level == "none":
        return False

    latest_command = _latest_command_execution(resolution_state)
    if latest_command is not None and _command_succeeded(latest_command):
        # A setup command (chmod, mkdir, cp, etc.) enables subsequent work but does NOT
        # verify the task objective. When the task explicitly requires repo-local
        # verification, fall through so the actual test/build command still runs.
        if _task_explicitly_requests_repo_local_verification(request) and _command_is_setup_only(
            latest_command
        ):
            pass  # treat as if no real verification has run yet
        else:
            # Command already ran and passed — no further verification needed.
            return False

    # Past this point, latest_command is either None (no verification yet) or a
    # failed command. In both cases, running or re-running verification would
    # materially improve the evidence for any task type that requires it.

    if _is_documentation_like_task(request):
        if _task_explicitly_requests_repo_local_verification(request):
            # Explicit verification requested: needed if no command ran, or if
            # the previous command failed (re-verification after repair).
            return latest_command is None or not _command_succeeded(latest_command)

        if _changed_files_look_documentation_only(resolution_state):
            return False

        if resolution_state.evidence.changed_files:
            return False

    if _task_explicitly_requests_repo_local_verification(request):
        return latest_command is None or not _command_succeeded(latest_command)

    if resolution_state.evidence.changed_files and _is_executable_implementation_like_task(request):
        # Re-verification is needed both when no command has run yet and when
        # the previous command failed (e.g. a repair pass was made after failure).
        return latest_command is None or not _command_succeeded(latest_command)

    return False


def _failed_verification_is_repairable_by_repo_changes(
    *,
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    command,
) -> bool:
    """
    Return True when the latest failed verification command indicates an error
    that can be repaired by changing repository files.

    Priority 1 — Structured error diagnosis (Phase 2/3):
      When an error_diagnosis observation is present in evidence, use its
      `is_recoverable` and `overall_error_class` fields instead of string-matching.
      This is more reliable because it reflects a two-step LLM analysis of the
      actual failure output with access to referenced file contents.

    Priority 2 — String-matching fallback:
      When no structured diagnosis is available (tool failed or wasn't called),
      fall back to heuristic markers in stdout/stderr.  This preserves the
      original behaviour for cases where the diagnostic tool is unavailable.

    In both paths, the function only returns True when repository files have
    already been changed (resolution_state.evidence.changed_files is non-empty),
    which ensures the repair signal is grounded in real evidence.
    """
    if command is None:
        return False

    if _command_succeeded(command):
        return False

    # --- Priority 1: structured diagnosis ---
    diagnosis = _latest_error_diagnosis_observation(resolution_state.evidence)
    if diagnosis is not None:
        is_recoverable: bool = bool(diagnosis.get("is_recoverable", True))
        if not is_recoverable:
            # Explicitly non-recoverable (network, permission, command_not_found, etc.)
            return False

        fault_side: str = diagnosis.get("fault_side") or "uncertain"

        # Environment failures are recoverable but NOT by changing repository files.
        # They are handled by environment_manager_agent, not code_change_agent.
        if fault_side == "environment":
            return False

        # Implementation / test / both failures are repairable by repo changes when
        # there are already changed files that provide a basis for the repair.
        if fault_side in ("implementation", "test", "both"):
            return bool(resolution_state.evidence.changed_files)

        # "uncertain" or missing fault_side → fall through to string-matching below.
        # If there are changed files we optimistically treat it as repairable.
        return bool(resolution_state.evidence.changed_files)

    # --- Priority 2: string-matching fallback ---
    stdout = (getattr(command, "stdout", "") or "").lower()
    stderr = (getattr(command, "stderr", "") or "").lower()
    summary = (getattr(command, "observed_outcome_summary", "") or "").lower()
    combined = "\n".join([stdout, stderr, summary])

    repairable_markers = (
        "modulenotfounderror",
        "importerror",
        "no module named",
        "cannot import",
        "ran 0 tests",
        "no tests ran",
        "collected 0 items",
        "error collecting",
        "failed to import",
    )

    if any(marker in combined for marker in repairable_markers):
        return bool(resolution_state.evidence.changed_files)

    return False


def _latest_step_failure_indicates_repairable_gap(
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    runtime_state: ExecutionState,
) -> bool:
    if not resolution_state.failed_steps:
        return False

    last_attempted_subagent = _last_attempted_subagent_name(runtime_state)

    if last_attempted_subagent == "context_selection_agent":
        return True

    if last_attempted_subagent in {
        "code_change_agent",
        "document_writer_agent",
        "test_builder_agent",
    }:
        return True

    if last_attempted_subagent == "environment_manager_agent":
        # Distinguish between two cases:
        # (a) env_manager SUCCEEDED: only prior command_runner failures remain in failed_steps.
        #     There is a real gap — command_runner needs to re-run to verify the fixed env.
        # (b) env_manager FAILED (SubagentRejectedStepError): its step ID is in failed_steps.
        #     This is a terminal condition; no repair subagent can resolve a failed install.
        #     (In practice Fix 4 returns immediately on env_manager failure, so this path
        #     acts as defense-in-depth only.)
        env_manager_step_failed = any(
            "environment_manager_agent" in s for s in resolution_state.failed_steps
        )
        return not env_manager_step_failed

    if last_attempted_subagent == "command_runner_agent":
        latest_command = _latest_command_execution(resolution_state)

        if latest_command is None:
            return False

        if _command_succeeded(latest_command):
            return False

        # Environment failures are repairable (by environment_manager_agent) even though
        # _failed_verification_is_repairable_by_repo_changes returns False for them.
        diagnosis = _latest_error_diagnosis_observation(resolution_state.evidence)
        if diagnosis is not None:
            fault_side = diagnosis.get("fault_side") or "uncertain"
            is_recoverable = bool(diagnosis.get("is_recoverable", True))
            if fault_side == "environment" and is_recoverable:
                return True

        return _failed_verification_is_repairable_by_repo_changes(
            request=request,
            resolution_state=resolution_state,
            command=latest_command,
        )

    return False


def _build_completion_checklist(
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    runtime_state: ExecutionState,
) -> dict[str, bool]:
    context_ready = resolution_state.phase != "discovery" or bool(resolution_state.completed_steps)

    implementation_needed = _task_requires_material_changes(request)
    implementation_done_if_needed = (
        not implementation_needed
        or bool(resolution_state.evidence.changed_files)
        or bool(resolution_state.evidence.artifacts_created)
        # A file-writing subagent completing a step is direct evidence that the
        # implementation pass ran, even when keyword detection is ambiguous
        or _code_change_agent_completed_a_step(resolution_state)
        or _document_writer_agent_completed_a_step(resolution_state)
        or _test_builder_agent_completed_a_step(resolution_state)
    )

    verification_needed = _verification_would_materially_improve(request, resolution_state)
    latest_command = _latest_command_execution(resolution_state)
    # A setup command (chmod +x, mkdir, etc.) does not constitute verification of the
    # task objective even when it succeeds. Exclude it from the "verification done" check.
    latest_cmd_is_real_verification = (
        latest_command is not None
        and _command_succeeded(latest_command)
        and not (
            _task_explicitly_requests_repo_local_verification(request)
            and _command_is_setup_only(latest_command)
        )
    )
    local_verification_done_if_material = not verification_needed or latest_cmd_is_real_verification

    new_concrete_gap_detected = _latest_step_failure_indicates_repairable_gap(
        request,
        resolution_state,
        runtime_state,
    )

    return {
        "context_ready": context_ready,
        "implementation_done_if_needed": implementation_done_if_needed,
        "local_verification_done_if_material": local_verification_done_if_material,
        "new_concrete_gap_detected": new_concrete_gap_detected,
    }


def _checklist_requires_finish(checklist: dict[str, bool]) -> bool:
    return (
        checklist["context_ready"]
        and checklist["implementation_done_if_needed"]
        and checklist["local_verification_done_if_material"]
        and not checklist["new_concrete_gap_detected"]
    )


def _build_operational_state_summary(
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    runtime_state: ExecutionState,
) -> dict:
    last_completed_subagent = _last_completed_subagent_name(resolution_state)
    last_attempted_subagent = _last_attempted_subagent_name(runtime_state)
    checklist = _build_completion_checklist(request, resolution_state, runtime_state)
    latest_command = _latest_command_execution(resolution_state)

    return {
        "phase": resolution_state.phase,
        "has_historical_context": resolution_state.historical_task_selection is not None,
        "has_changed_files": bool(resolution_state.evidence.changed_files),
        "has_command_evidence": bool(resolution_state.evidence.commands),
        "has_any_outputs": resolution_state.has_outputs(),
        "last_attempted_subagent_name": last_attempted_subagent,
        "last_completed_subagent_name": last_completed_subagent,
        "completed_step_count": len(resolution_state.completed_steps),
        "failed_step_count": len(resolution_state.failed_steps),
        "changed_file_count": len(resolution_state.evidence.changed_files),
        "command_count": len(resolution_state.evidence.commands),
        "artifact_count": len(resolution_state.evidence.artifacts_created),
        "risk_flag_count": len(resolution_state.risk_flags),
        "has_completed_any_step": bool(resolution_state.completed_steps),
        "has_failed_any_step": bool(resolution_state.failed_steps),
        "task_is_documentation_like": _is_documentation_like_task(request),
        "task_is_executable_implementation_like": _is_executable_implementation_like_task(request),
        "verification_explicitly_requested": _task_explicitly_requests_repo_local_verification(
            request
        ),
        "changed_files_look_documentation_only": _changed_files_look_documentation_only(
            resolution_state
        ),
        "latest_command_succeeded": _command_succeeded(latest_command) if latest_command else None,
        "completion_checklist": checklist,
    }


def _build_orchestrator_prompt(
    request: ExecutionRequest,
    runtime_state: ExecutionState,
    resolution_state: ResolutionState,
) -> str:
    capability_text = render_executor_capabilities_for_prompt(request.executor_type)

    historical_context_present = request.historical_context is not None
    historical_task_run_count = (
        len(request.historical_context.selected_task_runs)
        if request.historical_context is not None
        else 0
    )
    last_completed_subagent = _last_completed_subagent_name(resolution_state)
    last_attempted_subagent = _last_attempted_subagent_name(runtime_state)
    checklist = _build_completion_checklist(request, resolution_state, runtime_state)

    prompt_loader.validate_builder_inputs(
        "orchestrator",
        "decision",
        {
            "task_id": request.task_id,
            "task_type": request.task_type,
            "task_title": request.task_title,
            "task_description": request.task_description,
            "objective": request.objective,
            "acceptance_criteria": request.acceptance_criteria,
            "technical_constraints": request.technical_constraints,
            "out_of_scope": request.out_of_scope,
            "executor_type": request.executor_type,
            "execution_engine_capabilities": capability_text,
            "step_count": runtime_state.step_count,
            "agent_call_count": runtime_state.agent_call_count,
            "repair_attempt_count": runtime_state.repair_attempt_count,
            "historical_context_present": historical_context_present,
            "historical_task_run_count": historical_task_run_count,
            "relevant_files": request.context.relevant_files,
            "key_decisions": request.context.key_decisions,
            "related_tasks_count": len(request.context.related_tasks),
            "orchestration_phase": resolution_state.phase,
            "historical_task_selection_present": resolution_state.historical_task_selection
            is not None,
            "materialization_attempt_count": resolution_state.materialization_attempt_count,
            "completed_steps": resolution_state.completed_steps,
            "failed_steps": resolution_state.failed_steps,
            "last_attempted_subagent_name": last_attempted_subagent,
            "last_completed_subagent_name": last_completed_subagent,
            "risk_flags": resolution_state.risk_flags,
            "step_notes": resolution_state.step_notes,
            "operational_state_summary": _build_operational_state_summary(
                request, resolution_state, runtime_state
            ),
            "checklist_context_ready": checklist["context_ready"],
            "checklist_implementation_done_if_needed": checklist["implementation_done_if_needed"],
            "checklist_local_verification_done_if_material": checklist[
                "local_verification_done_if_material"
            ],
            "checklist_new_concrete_gap_detected": checklist["new_concrete_gap_detected"],
            "latest_error_diagnosis": _render_error_diagnosis_for_prompt(resolution_state.evidence),
            "changed_files": [
                item.model_dump() for item in resolution_state.evidence.changed_files
            ],
            "executed_commands": _render_commands_for_prompt(resolution_state),
            "files_read": _render_files_read_for_prompt(resolution_state),
            "change_dependencies": _render_change_dependencies_for_prompt(resolution_state),
            "artifacts_created": _render_artifacts_created_for_prompt(resolution_state),
            "evidence_notes": _render_notes_for_prompt(resolution_state),
            "evidence_items": _render_evidence_items_for_prompt(resolution_state),
        },
    )
    return f"""
Task:
- task_id: {request.task_id}
- task_type: {request.task_type}
- title: {request.task_title}
- description: {request.task_description}
- objective: {request.objective}
- acceptance_criteria: {request.acceptance_criteria}
- technical_constraints: {request.technical_constraints}
- out_of_scope: {request.out_of_scope}
- executor_type: {request.executor_type}

Execution engine capability catalog:
{capability_text}

Runtime counters:
- step_count: {runtime_state.step_count}
- agent_call_count: {runtime_state.agent_call_count}
- repair_attempt_count: {runtime_state.repair_attempt_count}

Current request state:
- historical_context_present: {historical_context_present}
- historical_task_run_count: {historical_task_run_count}
- relevant_files: {request.context.relevant_files}
- key_decisions: {request.context.key_decisions}
- related_tasks_count: {len(request.context.related_tasks)}

Current orchestration state:
- phase: {resolution_state.phase}
- historical_task_selection_present: {resolution_state.historical_task_selection is not None}
- materialization_attempt_count: {resolution_state.materialization_attempt_count}
- completed_steps: {resolution_state.completed_steps}
- failed_steps: {resolution_state.failed_steps}
- last_attempted_subagent_name: {last_attempted_subagent}
- last_completed_subagent_name: {last_completed_subagent}
- risk_flags: {resolution_state.risk_flags}
- step_notes: {resolution_state.step_notes}
- operational_state_summary: {_build_operational_state_summary(request, resolution_state, runtime_state)}

Completion checklist (use as a reasoning aid, not a hard rule):
- context_ready: {"yes" if checklist["context_ready"] else "no"}
- implementation_done_if_needed: {"yes" if checklist["implementation_done_if_needed"] else "no"}
- local_verification_done_if_material: {"yes" if checklist["local_verification_done_if_material"] else "no"}
- new_concrete_gap_detected: {"yes" if checklist["new_concrete_gap_detected"] else "no"}

Checklist guidance:
- When the first three fields are yes and the last is no, that is a strong signal to finish — but always verify against task_type and the actual accumulated evidence before deciding.
- For implementation or configuration task types, do not finish unless at least one of code_change_agent or command_runner_agent has executed.
- For testing task types:
  * Do not finish unless at least one of test_builder_agent or code_change_agent has executed.
  * When verification_level="runtime", local_verification_done_if_material requires
    command_runner_agent to have executed successfully — test_builder_agent completing alone
    does not satisfy it. If no command has run yet after test_builder_agent, continue.
  * If command_runner_agent failed with a repairable error and new_concrete_gap_detected=yes,
    consult error_diagnosis.fault_side to pick the repair subagent:
      fault_side="implementation" → code_change_agent → command_runner_agent
      fault_side="test"           → test_builder_agent → command_runner_agent
      fault_side="both"           → code_change_agent → command_runner_agent
      fault_side="environment"    → environment_manager_agent → command_runner_agent
      fault_side="uncertain"      → use overall_error_class and is_recoverable to decide
    Do not finish here — call the appropriate repair subagent first.
- A checklist that looks satisfied but contradicts the task_type or the visible evidence means a subagent step was likely skipped — call the appropriate subagent instead of finishing.

Latest error diagnosis (structured, from error_diagnostic_tool — use for routing decisions):
{_render_error_diagnosis_for_prompt(resolution_state.evidence)}

Accumulated execution evidence:
- changed_files: {[item.model_dump() for item in resolution_state.evidence.changed_files]}
- executed_commands: {_render_commands_for_prompt(resolution_state)}
- files_read: {_render_files_read_for_prompt(resolution_state)}
- change_dependencies: {_render_change_dependencies_for_prompt(resolution_state)}
- artifacts_created: {_render_artifacts_created_for_prompt(resolution_state)}
- evidence_notes: {_render_notes_for_prompt(resolution_state)}
- evidence_items: {_render_evidence_items_for_prompt(resolution_state)}

Decision reminders:
- If no subagent has acted yet, call context_selection_agent.
- You must not call the same subagent as the last attempted subagent.
- In discovery, the task is still preparing context.
- In execution, decide which subagent can still provide the best next operational progress.
- Do not require repository-local verification merely because files changed.
- Documentation/specification/requirements tasks usually finish after the material document is produced unless the task explicitly asks for executable repo-local verification.
- Do not continue the loop just to improve or polish an already sufficient output.
- A warning or risk note is not a concrete gap by itself.
- If the latest verification succeeded and materially covers the task, that usually supports finish.
- Finish as soon as the checklist says the pass is sufficient.
- Reject only if no safe operational contribution is possible.
""".strip()


def _invalidate_decision(
    decision: NextActionDecision,
    *,
    rationale: str,
    expected_outcome: str,
    extra_risk_flag: str,
) -> NextActionDecision:
    return NextActionDecision(
        decision_type=DECISION_INVALID,
        rationale=rationale,
        subagent_name=None,
        target_paths=[],
        expected_outcome=expected_outcome,
        risk_flags=list(decision.risk_flags) + [extra_risk_flag],
    )


def _normalize_decision(
    request: ExecutionRequest,
    state: ResolutionState,
    runtime_state: ExecutionState,
    decision: NextActionDecision,
) -> NextActionDecision:
    last_attempted_subagent = _last_attempted_subagent_name(runtime_state)
    allowed_subagents = _allowed_subagents_for_phase(state.phase)

    if not runtime_state.agent_call_count:
        if not (
            decision.decision_type == DECISION_CALL_SUBAGENT
            and decision.subagent_name == "context_selection_agent"
        ):
            return _invalidate_decision(
                decision,
                rationale=(
                    "No subagent has acted yet. The first valid decision must call "
                    "context_selection_agent to initialize execution context."
                ),
                expected_outcome="Retry orchestration with an initial context selection step.",
                extra_risk_flag="initial_context_selection_required",
            )

    if state.phase == "discovery":
        if not (
            decision.decision_type == DECISION_CALL_SUBAGENT
            and decision.subagent_name == "context_selection_agent"
        ):
            return _invalidate_decision(
                decision,
                rationale="Discovery phase only allows calling context_selection_agent.",
                expected_outcome="Retry orchestration with a valid discovery-phase decision.",
                extra_risk_flag="invalid_decision_for_discovery_phase",
            )

    if decision.decision_type == DECISION_CALL_SUBAGENT:
        if decision.subagent_name not in allowed_subagents:
            return _invalidate_decision(
                decision,
                rationale=(
                    f"Subagent '{decision.subagent_name}' is not allowed in phase '{state.phase}'."
                ),
                expected_outcome="Retry orchestration with a structurally valid subagent.",
                extra_risk_flag="invalid_subagent_for_phase",
            )

        if (
            last_attempted_subagent is not None
            and decision.subagent_name == last_attempted_subagent
        ):
            # Exception: allow command_runner_agent to run again immediately after a
            # setup-only command (chmod, mkdir, etc.) when the task explicitly requires
            # verification. The setup step did not consume the verification slot.
            _allow_repeat = (
                decision.subagent_name == "command_runner_agent"
                and _task_explicitly_requests_repo_local_verification(request)
                and _command_is_setup_only(_latest_command_execution(state))
            )
            if not _allow_repeat:
                return _invalidate_decision(
                    decision,
                    rationale=(
                        f"Subagent '{decision.subagent_name}' cannot be called twice in a row."
                    ),
                    expected_outcome=(
                        "Retry orchestration with a different subagent, finish, or reject."
                    ),
                    extra_risk_flag="same_subagent_twice_in_a_row",
                )

    if decision.decision_type == DECISION_FINISH and not state.completed_steps:
        return _invalidate_decision(
            decision,
            rationale="Cannot finish before any subagent has completed work.",
            expected_outcome="Retry orchestration with a valid first subagent call.",
            extra_risk_flag="finish_without_any_completed_step",
        )

    # Guard: if the last command_runner_agent verification failed with markers that indicate
    # a repairable repository gap (ImportError, ModuleNotFoundError, collection error, etc.),
    # block finish so the orchestrator is forced to call code_change_agent for a repair pass.
    # This prevents the LLM from rationalizing test import failures as "environment issues"
    # and finishing prematurely when a repository change could actually close the gap.
    # Narrowly scoped to command_runner_agent failures only — context_selection and
    # code_change failures are handled separately by the existing step-failure logic.
    # _normalize_decision is only reached when the budget is not exhausted, so blocking
    # finish here is safe — _maybe_build_forced_terminal_decision handles budget exits.
    if decision.decision_type == DECISION_FINISH:
        _last_attempted = _last_attempted_subagent_name(runtime_state)
        if _last_attempted == "command_runner_agent":
            _latest_cmd = _latest_command_execution(state)
            if (
                _latest_cmd is not None
                and not _command_succeeded(_latest_cmd)
                and _failed_verification_is_repairable_by_repo_changes(
                    request=request,
                    resolution_state=state,
                    command=_latest_cmd,
                )
            ):
                return _invalidate_decision(
                    decision,
                    rationale=(
                        "The last verification command failed with output that is programmatically "
                        "identified as repairable by repository changes (e.g. ImportError, "
                        "ModuleNotFoundError, collection error). Finishing now would skip the "
                        "required repair pass. Call code_change_agent to address the gap."
                    ),
                    expected_outcome=(
                        "Retry orchestration by calling code_change_agent to repair the "
                        "repository gap indicated by the last failed verification command."
                    ),
                    extra_risk_flag="finish_blocked_by_repairable_verification_gap",
                )

    return decision


def _latest_error_diagnosis_observation(evidence: ExecutionEvidence) -> dict | None:
    """
    Return the payload dict of the most recent error_diagnosis observation, or None.

    Scans observations in reverse so the most recently appended item is returned first.
    """
    for item in reversed(evidence.observations):
        if item.evidence_type == OBSERVATION_TYPE_ERROR_DIAGNOSIS:
            return item.payload
    return None


def _extract_error_diagnoses_for_trace(evidence: ExecutionEvidence) -> list[dict]:
    """Extract all error_diagnosis observations from evidence as compact dicts."""
    diagnoses: list[dict] = []
    for item in evidence.observations:
        if item.evidence_type == OBSERVATION_TYPE_ERROR_DIAGNOSIS:
            p = item.payload
            diagnoses.append(
                {
                    "fault_side": p.get("fault_side", "uncertain"),
                    "confidence": p.get("confidence", "medium"),
                    "is_recoverable": p.get("is_recoverable", True),
                    "overall_error_class": p.get("overall_error_class"),
                }
            )
    return diagnoses


def _count_trace_events(
    orchestrator_trace: OrchestratorTrace | None,
    event_type: str,
) -> int:
    if orchestrator_trace is None:
        return 0
    return sum(1 for e in orchestrator_trace.events if e.event_type == event_type)


def _write_orchestrator_execution_trace(
    *,
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    runtime_state: ExecutionState,
    budget: LoopBudget,
    executed_subagents: list[str],
    final_decision: str,
    forced_terminal: bool,
) -> None:
    """Append one orchestrator run summary to execution_trace.jsonl.

    Never raises — trace failures must not interrupt execution.
    """
    from app.services.supervisor.trace_writer import append_execution_trace

    entry: dict = {
        "agent": "orchestrator",
        "project_id": request.project_id,
        "task_id": request.task_id,
        "run_id": request.execution_run_id,
        "execution_agent_sequence": list(executed_subagents),
        "steps_used": runtime_state.step_count,
        "agent_calls_used": runtime_state.agent_call_count,
        "repair_attempts_used": runtime_state.repair_attempt_count,
        "final_decision": final_decision,
        "forced_terminal": forced_terminal,
        "budget_max": {
            "max_steps": budget.max_steps,
            "max_agent_calls": budget.max_agent_calls,
            "max_repair_attempts": budget.max_repair_attempts,
        },
        "invalid_decision_count": _count_trace_events(
            resolution_state.orchestrator_trace, "orchestrator_decision_invalidated"
        ),
        "normalized_decision_count": _count_trace_events(
            resolution_state.orchestrator_trace, "decision_normalized_by_guardrail"
        ),
        "error_diagnoses": _extract_error_diagnoses_for_trace(resolution_state.evidence),
    }
    append_execution_trace(project_id=request.project_id, entry=entry)


def _expand_context_from_error_diagnosis(
    *,
    resolution_state: ResolutionState,
) -> bool:
    """
    Inspect the latest error_diagnosis observation for newly_discovered_files that are not
    yet loaded in preloaded_dependency_files.  Read each missing file from the workspace
    overlay (priority) or source baseline, then call replace_execution_request() so the
    updated context is visible to all subsequent subagents and orchestrator decisions.

    Called once at the top of every orchestrator loop iteration, before building the prompt
    or checking forced-terminal conditions.

    Returns True when at least one file was newly loaded, False when no expansion occurred.
    The expansion is idempotent: files already present in preloaded_dependency_files are
    skipped without re-reading.
    """
    diagnosis_payload = _latest_error_diagnosis_observation(resolution_state.evidence)
    if not diagnosis_payload:
        return False

    newly_discovered: list[str] = diagnosis_payload.get("newly_discovered_files") or []
    if not newly_discovered:
        return False

    active_request = resolution_state.execution_request
    already_loaded = set(active_request.context.preloaded_dependency_files.keys())
    missing = [p.strip() for p in newly_discovered if p and p.strip() not in already_loaded]
    if not missing:
        return False

    workspace_root = active_request.context.workspace_path
    source_root = active_request.context.source_path

    new_files: dict[str, str] = {}
    for rel_path in missing:
        for base in filter(None, [workspace_root, source_root]):
            base_path = Path(base).resolve()
            candidate = (base_path / rel_path).resolve()
            try:
                candidate.relative_to(base_path)
            except ValueError:
                continue
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                    new_files[rel_path] = content
                    break
                except Exception:
                    pass

    if not new_files:
        return False

    expanded = {**active_request.context.preloaded_dependency_files, **new_files}
    new_context = active_request.context.model_copy(update={"preloaded_dependency_files": expanded})
    new_request = active_request.model_copy(update={"context": new_context})
    resolution_state.replace_execution_request(new_request)

    logger.info(
        "orchestrator_error_diagnosis_context_expanded task_id=%s new_files_count=%s paths=%s",
        active_request.task_id,
        len(new_files),
        list(new_files.keys()),
    )
    return True


def _render_error_diagnosis_for_prompt(evidence: ExecutionEvidence) -> str:
    """
    Return a concise, prompt-ready summary of the latest error_diagnosis observation.
    Returns a placeholder string when no diagnosis is present.
    """
    diagnosis = _latest_error_diagnosis_observation(evidence)
    if not diagnosis:
        return "latest_error_diagnosis: none"

    overall_error_class = diagnosis.get("overall_error_class", "unknown")
    fault_side = diagnosis.get("fault_side", "uncertain")
    confidence = diagnosis.get("confidence", "medium")
    is_recoverable = diagnosis.get("is_recoverable", True)
    repair_directive = diagnosis.get("repair_directive", "")
    cross_file_root_cause = diagnosis.get("cross_file_root_cause")
    newly_discovered_files = diagnosis.get("newly_discovered_files") or []

    root_cause_errors = [
        e for e in (diagnosis.get("root_cause_errors") or []) if not e.get("is_cascade")
    ]
    cascade_errors = [e for e in (diagnosis.get("root_cause_errors") or []) if e.get("is_cascade")]

    lines = [
        "latest_error_diagnosis:",
        f"  overall_error_class: {overall_error_class}",
        f"  fault_side: {fault_side}",
        f"  confidence: {confidence}",
        f"  is_recoverable: {is_recoverable}",
        f"  repair_directive: {repair_directive}",
        f"  cross_file_root_cause: {cross_file_root_cause or 'null'}",
        f"  newly_discovered_files_count: {len(newly_discovered_files)}",
        f"  root_cause_errors_count: {len(root_cause_errors)}",
        f"  cascade_errors_count: {len(cascade_errors)}",
    ]

    if root_cause_errors:
        lines.append("  root_cause_errors:")
        for err in root_cause_errors[:3]:  # cap at 3 to avoid prompt bloat
            lines.append(f"    - file: {err.get('file_path', 'unknown')}")
            lines.append(f"      message: {err.get('message', '')}")
            lines.append(f"      error_type: {err.get('error_type', '')}")

    if newly_discovered_files:
        lines.append("  newly_discovered_files (already loaded into context):")
        for path in newly_discovered_files[:8]:  # cap at 8
            lines.append(f"    - {path}")

    return "\n".join(lines)


def _build_terminal_decision(
    *,
    rationale: str,
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    runtime_state: ExecutionState,
) -> NextActionDecision:
    checklist = _build_completion_checklist(request, resolution_state, runtime_state)
    if _checklist_requires_finish(checklist) and resolution_state.completed_steps:
        return NextActionDecision(
            decision_type=DECISION_FINISH,
            rationale=rationale,
            expected_outcome="Stop orchestration because the current operational pass is sufficient.",
            risk_flags=[],
        )

    return NextActionDecision(
        decision_type=DECISION_REJECT,
        rationale=rationale,
        expected_outcome="Stop orchestration because no safe further progress is available.",
        risk_flags=list(resolution_state.risk_flags),
    )


def _maybe_build_forced_terminal_decision(
    *,
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    runtime_state: ExecutionState,
    budget: LoopBudget,
    consecutive_invalid_decisions: int,
) -> NextActionDecision | None:
    last_attempted_subagent = _last_attempted_subagent_name(runtime_state)
    latest_command = _latest_command_execution(resolution_state)

    # environment_manager_agent is intentionally excluded from this set.
    # It installs packages but does NOT write repository files — it must not be treated
    # as a file-writing pass, otherwise the forced-FINISH logic fires before
    # command_runner_agent can re-verify the environment.
    _file_writing_agents = {
        "code_change_agent",
        "document_writer_agent",
        "test_builder_agent",
    }

    if (
        last_attempted_subagent in _file_writing_agents
        and bool(resolution_state.evidence.changed_files)
        and _verification_would_materially_improve(request, resolution_state) is False
        and not _latest_step_failure_indicates_repairable_gap(
            request, resolution_state, runtime_state
        )
        and resolution_state.completed_steps
    ):
        return NextActionDecision(
            decision_type=DECISION_FINISH,
            rationale=(
                "A material repository output already exists and no further operational step is required."
            ),
            expected_outcome="Stop orchestration because the current operational pass is sufficient.",
            risk_flags=[],
        )

    if (
        last_attempted_subagent == "command_runner_agent"
        and latest_command is not None
        and _command_succeeded(latest_command)
        and bool(resolution_state.evidence.changed_files)
        and resolution_state.completed_steps
        and not (
            _task_explicitly_requests_repo_local_verification(request)
            and _command_is_setup_only(latest_command)
        )
    ):
        return NextActionDecision(
            decision_type=DECISION_FINISH,
            rationale=(
                "A material repository output exists and the latest repository-local verification succeeded, "
                "so the operational pass should finish."
            ),
            expected_outcome="Stop orchestration because the current operational pass is sufficient.",
            risk_flags=[],
        )

    if (
        last_attempted_subagent == "command_runner_agent"
        and latest_command is None
        and bool(resolution_state.evidence.changed_files)
        and _verification_would_materially_improve(request, resolution_state) is False
        and not _latest_step_failure_indicates_repairable_gap(
            request, resolution_state, runtime_state
        )
        and resolution_state.completed_steps
    ):
        return NextActionDecision(
            decision_type=DECISION_FINISH,
            rationale=(
                "A verification step was attempted, but repository-local verification is not materially "
                "required for the current task and a material repository output already exists."
            ),
            expected_outcome="Stop orchestration because another verification loop would be redundant.",
            risk_flags=[],
        )

    if (
        last_attempted_subagent in _file_writing_agents
        and not resolution_state.evidence.changed_files
        and not resolution_state.failed_steps
        and resolution_state.completed_steps
    ):
        return NextActionDecision(
            decision_type=DECISION_FINISH,
            rationale=(
                "The last file-writing pass completed without materialized file changes and no concrete "
                "operational gap remains, so the orchestration should stop instead of looping."
            ),
            expected_outcome="Stop orchestration because another immediate file-writing pass would be redundant.",
            risk_flags=[],
        )

    if runtime_state.agent_call_count >= budget.max_agent_calls:
        return _build_terminal_decision(
            rationale=(
                "The orchestrator reached max_agent_calls and must stop with the best terminal decision "
                "supported by the current checklist and evidence."
            ),
            request=request,
            resolution_state=resolution_state,
            runtime_state=runtime_state,
        )

    if (
        runtime_state.repair_attempt_count >= budget.max_repair_attempts
        or consecutive_invalid_decisions >= 3
    ):
        return _build_terminal_decision(
            rationale=(
                "The orchestrator exhausted its repair budget after repeated invalid decisions, so it must stop "
                "with the safest terminal decision."
            ),
            request=request,
            resolution_state=resolution_state,
            runtime_state=runtime_state,
        )

    return None


class ExecutionOrchestrator:
    def __init__(
        self,
        *,
        runtime: BaseAgentRuntime,
        registry: SubagentRegistry,
        budget: LoopBudget,
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.budget = budget

    def run(
        self, db: Session, request: ExecutionRequest
    ) -> tuple[ExecutionResult, ExecutionRequest]:
        runtime_state = ExecutionState()
        resolution_state = ResolutionState(
            execution_request=request,
            orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        )
        executed_subagents: list[str] = []
        consecutive_invalid_decisions = 0

        active_request = resolution_state.execution_request

        resolution_state.orchestrator_trace.add_event(
            event_type="orchestrator_started",
            step_count=runtime_state.step_count,
            task_id=active_request.task_id,
            payload={
                "title": active_request.task_title,
                "executor_type": active_request.executor_type,
                "max_steps": self.budget.max_steps,
                "max_agent_calls": self.budget.max_agent_calls,
                "max_repair_attempts": self.budget.max_repair_attempts,
                "registered_subagents": self.registry.all_names(),
            },
        )

        logger.info(
            "execution_orchestrator_started task_id=%s executor_type=%s max_steps=%s max_agent_calls=%s max_repair_attempts=%s",
            active_request.task_id,
            active_request.executor_type,
            self.budget.max_steps,
            self.budget.max_agent_calls,
            self.budget.max_repair_attempts,
        )

        while runtime_state.step_count < self.budget.max_steps:
            # Expand context from any unprocessed error_diagnosis observations before
            # building the prompt or checking forced-terminal conditions.
            _expand_context_from_error_diagnosis(resolution_state=resolution_state)

            active_request = resolution_state.execution_request

            forced_terminal_decision = _maybe_build_forced_terminal_decision(
                request=active_request,
                resolution_state=resolution_state,
                runtime_state=runtime_state,
                budget=self.budget,
                consecutive_invalid_decisions=consecutive_invalid_decisions,
            )
            if forced_terminal_decision is not None:
                decision = forced_terminal_decision
                _this_decision_was_forced = True
            else:
                try:
                    raw_decision = self._decide_next_action(
                        request=active_request,
                        runtime_state=runtime_state,
                        resolution_state=resolution_state,
                    )
                except ExecutionEngineRejectedError as exc:
                    if exc.failure_code == "invalid_next_action_output" and (
                        resolution_state.evidence.changed_files
                        or resolution_state.evidence.commands
                    ):
                        _append_trace_notes_to_evidence(resolution_state)
                        logger.warning(
                            "execution_orchestrator_completed_on_invalid_output task_id=%s changed_files=%s commands=%s",
                            active_request.task_id,
                            len(resolution_state.evidence.changed_files),
                            len(resolution_state.evidence.commands),
                        )
                        return (
                            ExecutionResult(
                                task_id=active_request.task_id,
                                decision=EXECUTION_DECISION_COMPLETED,
                                summary="Orchestrator produced all available evidence; coordination loop could not determine next step.",
                                details=exc.message,
                                remaining_scope=exc.remaining_scope,
                                blockers_found=exc.blockers_found,
                                validation_notes=[
                                    "Orchestrator decision loop interrupted by invalid output after subagent work was completed; evidence forwarded to validation.",
                                    *(exc.validation_notes or []),
                                ],
                                execution_agent_sequence=list(executed_subagents),
                                evidence=resolution_state.evidence,
                            ),
                            active_request,
                        )
                    raise
                decision = _normalize_decision(
                    request=active_request,
                    state=resolution_state,
                    runtime_state=runtime_state,
                    decision=raw_decision,
                )
                _this_decision_was_forced = False

                if (
                    decision.decision_type != raw_decision.decision_type
                    or decision.subagent_name != raw_decision.subagent_name
                ):
                    logger.warning(
                        "execution_orchestrator_decision_normalized task_id=%s phase=%s original=%s/%s normalized=%s/%s rationale=%s",
                        active_request.task_id,
                        resolution_state.phase,
                        raw_decision.decision_type,
                        raw_decision.subagent_name,
                        decision.decision_type,
                        decision.subagent_name,
                        decision.rationale,
                    )
                    resolution_state.orchestrator_trace.add_event(
                        event_type="decision_normalized_by_guardrail",
                        step_count=runtime_state.step_count,
                        task_id=active_request.task_id,
                        payload={
                            "phase": resolution_state.phase,
                            "original_decision_type": raw_decision.decision_type,
                            "original_subagent_name": raw_decision.subagent_name,
                            "normalized_decision_type": decision.decision_type,
                            "normalized_subagent_name": decision.subagent_name,
                            "normalized_rationale": decision.rationale,
                        },
                    )

            logger.info(
                "execution_orchestrator_decision task_id=%s phase=%s decision_type=%s subagent_name=%s rationale=%s",
                active_request.task_id,
                resolution_state.phase,
                decision.decision_type,
                decision.subagent_name,
                decision.rationale,
            )

            resolution_state.orchestrator_trace.add_event(
                event_type="next_action_decided",
                step_count=runtime_state.step_count,
                task_id=active_request.task_id,
                payload=decision.model_dump(),
            )
            runtime_state.register_step()

            if decision.decision_type == DECISION_FINISH:
                resolution_state.orchestrator_trace.add_event(
                    event_type="orchestrator_finished",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "decision": decision.model_dump(),
                        "phase": resolution_state.phase,
                    },
                )
                _append_trace_notes_to_evidence(resolution_state)
                _write_orchestrator_execution_trace(
                    request=active_request,
                    resolution_state=resolution_state,
                    runtime_state=runtime_state,
                    budget=self.budget,
                    executed_subagents=executed_subagents,
                    final_decision="finish",
                    forced_terminal=_this_decision_was_forced,
                )

                logger.info(
                    "execution_orchestrator_finished task_id=%s changed_files=%s commands=%s repair_attempts=%s",
                    active_request.task_id,
                    len(resolution_state.evidence.changed_files),
                    len(resolution_state.evidence.commands),
                    runtime_state.repair_attempt_count,
                )

                return (
                    ExecutionResult(
                        task_id=active_request.task_id,
                        decision=EXECUTION_DECISION_COMPLETED,
                        summary="Operational execution loop completed successfully.",
                        details=decision.rationale,
                        completed_scope="Execution engine completed its current operational pass.",
                        remaining_scope=None,
                        blockers_found=[],
                        validation_notes=[
                            "Execution orchestrator finished normally.",
                        ],
                        execution_agent_sequence=list(executed_subagents),
                        evidence=resolution_state.evidence,
                    ),
                    active_request,
                )

            if decision.decision_type == DECISION_INVALID:
                consecutive_invalid_decisions += 1
                runtime_state.register_repair_attempt()
                resolution_state.mark_step_failed(
                    f"orchestrator_invalid_decision_{runtime_state.repair_attempt_count}"
                )
                resolution_state.add_note(
                    "The orchestrator proposed an invalid decision for the current state and must repair the orchestration decision."
                )
                resolution_state.add_risk_flags(decision.risk_flags)
                resolution_state.orchestrator_trace.add_event(
                    event_type="orchestrator_decision_invalidated",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        **decision.model_dump(),
                        "consecutive_invalid_decisions": consecutive_invalid_decisions,
                        "repair_attempt_count": runtime_state.repair_attempt_count,
                    },
                )
                resolution_state.evidence.add_note(
                    message=(
                        "Orchestrator decision was invalid for the current state. "
                        "This consumed one repair attempt."
                    ),
                    producer="execution_orchestrator",
                )
                logger.warning(
                    "execution_orchestrator_invalid_decision task_id=%s consecutive_invalid_decisions=%s repair_attempt_count=%s rationale=%s",
                    active_request.task_id,
                    consecutive_invalid_decisions,
                    runtime_state.repair_attempt_count,
                    decision.rationale,
                )
                continue

            consecutive_invalid_decisions = 0

            if decision.decision_type == DECISION_REJECT:
                remaining_scope = active_request.task_description or active_request.task_title
                blockers_found = list(
                    dict.fromkeys([*resolution_state.risk_flags, *decision.risk_flags])
                )

                resolution_state.orchestrator_trace.add_event(
                    event_type="orchestrator_rejected",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload=decision.model_dump(),
                )
                _append_trace_notes_to_evidence(resolution_state)
                _write_orchestrator_execution_trace(
                    request=active_request,
                    resolution_state=resolution_state,
                    runtime_state=runtime_state,
                    budget=self.budget,
                    executed_subagents=executed_subagents,
                    final_decision="reject",
                    forced_terminal=_this_decision_was_forced,
                )

                return (
                    ExecutionResult(
                        task_id=active_request.task_id,
                        decision=EXECUTION_DECISION_FAILED,
                        summary="Execution orchestrator rejected the task.",
                        details=decision.rationale,
                        remaining_scope=remaining_scope,
                        blockers_found=blockers_found,
                        validation_notes=["Execution orchestrator rejected the task."],
                        execution_agent_sequence=list(executed_subagents),
                        evidence=resolution_state.evidence,
                    ),
                    active_request,
                )

            try:
                step = self._build_step_from_decision(
                    decision=decision,
                    sequence_no=runtime_state.step_count,
                )

                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_selected",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "subagent_name": step.subagent_name,
                        "target_paths": step.target_paths,
                        "step_id": step.id,
                    },
                )

                subagent = self.registry.get(step.subagent_name)
                executed_subagents.append(subagent.name)
                runtime_state.register_agent_call(subagent.name)

                resolution_state = subagent.execute_step(
                    db=db,
                    request=active_request,
                    step=step,
                    state=resolution_state,
                )
                resolution_state.mark_step_completed(step.id)

                if resolution_state.needs_dependency_signal:
                    _append_trace_notes_to_evidence(resolution_state)
                    return (
                        ExecutionResult(
                            task_id=active_request.task_id,
                            decision=EXECUTION_DECISION_FAILED,
                            summary=(
                                f"Task requires an unavailable dependency: "
                                f"{resolution_state.needs_dependency_signal}"
                            ),
                            details=(
                                "A subagent signalled that the current runtime spec is missing "
                                "a required package. The task is routed to manual review for "
                                "package approval."
                            ),
                            remaining_scope=active_request.task_description
                            or active_request.task_title,
                            blockers_found=[
                                f"NEEDS_DEPENDENCY:{resolution_state.needs_dependency_signal}"
                            ],
                            validation_notes=[
                                "needs_dependency_signal raised — manual review required for package approval."
                            ],
                            execution_agent_sequence=list(executed_subagents),
                            evidence=resolution_state.evidence,
                        ),
                        active_request,
                    )

                _advance_phase_after_step(
                    resolution_state,
                    decision=decision,
                )

                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_completed",
                    step_count=runtime_state.step_count,
                    task_id=resolution_state.execution_request.task_id,
                    payload={
                        "subagent_name": step.subagent_name,
                        "phase": resolution_state.phase,
                        "changed_files_count": len(resolution_state.evidence.changed_files),
                        "commands_count": len(resolution_state.evidence.commands),
                    },
                )

            except SubagentRegistryError as exc:
                resolution_state.mark_step_failed(
                    f"registry_error_{decision.subagent_name or decision.decision_type}_{runtime_state.step_count}"
                )
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_registry_error",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={"error": str(exc)},
                )
                _append_trace_notes_to_evidence(resolution_state)
                return (
                    ExecutionResult(
                        task_id=active_request.task_id,
                        decision=EXECUTION_DECISION_FAILED,
                        summary=str(exc),
                        details="The orchestrator selected an unregistered subagent.",
                        remaining_scope=active_request.task_description
                        or active_request.task_title,
                        blockers_found=[str(exc)],
                        validation_notes=["Registry misconfiguration in orchestrator loop."],
                        execution_agent_sequence=list(executed_subagents),
                        evidence=resolution_state.evidence,
                    ),
                    active_request,
                )

            except SubagentRejectedStepError as exc:
                resolution_state.mark_step_failed(
                    f"subagent_rejected_{decision.subagent_name or decision.decision_type}_{runtime_state.step_count}"
                )
                resolution_state.add_risk_flags([str(exc)])
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_rejected_step",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "decision_type": decision.decision_type,
                        "subagent_name": decision.subagent_name,
                        "error": str(exc),
                    },
                )

                # environment_manager_agent rejects are terminal: a failed package install
                # cannot be resolved by calling any other subagent in the repair loop.
                # Route immediately to manual review rather than consuming repair budget.
                if decision.subagent_name == "environment_manager_agent":
                    _append_trace_notes_to_evidence(resolution_state)
                    _write_orchestrator_execution_trace(
                        request=active_request,
                        resolution_state=resolution_state,
                        runtime_state=runtime_state,
                        budget=self.budget,
                        executed_subagents=executed_subagents,
                        final_decision="env_manager_rejected",
                        forced_terminal=True,
                    )
                    logger.warning(
                        "execution_orchestrator_env_manager_rejected_terminal task_id=%s error=%s",
                        active_request.task_id,
                        str(exc),
                    )
                    return (
                        ExecutionResult(
                            task_id=active_request.task_id,
                            decision=EXECUTION_DECISION_FAILED,
                            summary=(
                                f"Environment package installation failed and cannot be "
                                f"resolved automatically: {str(exc)}"
                            ),
                            details=(
                                "The environment_manager_agent could not install the required "
                                "packages. This requires manual intervention (e.g. user approval "
                                "for a new package version or manual environment configuration)."
                            ),
                            remaining_scope=active_request.task_description
                            or active_request.task_title,
                            blockers_found=[f"ENV_INSTALL_FAILED:{str(exc)}"],
                            validation_notes=[
                                "environment_manager_agent rejected — package installation failed. "
                                "Task forwarded for manual review."
                            ],
                            execution_agent_sequence=list(executed_subagents),
                            evidence=resolution_state.evidence,
                        ),
                        active_request,
                    )

            except Exception as exc:
                logger.exception(
                    "execution_orchestrator_subagent_unexpected_error task_id=%s subagent=%s step=%s",
                    active_request.task_id,
                    decision.subagent_name,
                    runtime_state.step_count,
                )
                if _is_transient_infrastructure_error(exc):
                    raise ExecutionEngineTransientError(
                        f"Transient infrastructure error during subagent '{decision.subagent_name}': {exc}"
                    ) from exc
                resolution_state.mark_step_failed(
                    f"subagent_error_{decision.subagent_name or decision.decision_type}_{runtime_state.step_count}"
                )
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_unexpected_error",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "decision_type": decision.decision_type,
                        "subagent_name": decision.subagent_name,
                        "error": str(exc),
                    },
                )
                _append_trace_notes_to_evidence(resolution_state)
                return (
                    ExecutionResult(
                        task_id=active_request.task_id,
                        decision=EXECUTION_DECISION_FAILED,
                        summary=f"Unexpected orchestrator loop failure: {str(exc)}",
                        details="Unexpected exception inside execution orchestrator loop.",
                        remaining_scope=active_request.task_description
                        or active_request.task_title,
                        blockers_found=[str(exc)],
                        validation_notes=["Unexpected orchestrator loop exception."],
                        execution_agent_sequence=list(executed_subagents),
                        evidence=resolution_state.evidence,
                    ),
                    active_request,
                )

        active_request = resolution_state.execution_request

        resolution_state.orchestrator_trace.add_event(
            event_type="orchestrator_budget_exceeded",
            step_count=runtime_state.step_count,
            task_id=active_request.task_id,
            payload={"max_steps": self.budget.max_steps},
        )
        _append_trace_notes_to_evidence(resolution_state)
        _write_orchestrator_execution_trace(
            request=active_request,
            resolution_state=resolution_state,
            runtime_state=runtime_state,
            budget=self.budget,
            executed_subagents=executed_subagents,
            final_decision="budget_exceeded",
            forced_terminal=True,
        )

        return (
            ExecutionResult(
                task_id=active_request.task_id,
                decision=EXECUTION_DECISION_COMPLETED,
                summary="Execution budget exceeded; forwarding accumulated work to validation.",
                details=(
                    "The orchestrator loop exceeded max_steps. "
                    "Partial work is forwarded to validation for assessment."
                ),
                remaining_scope=active_request.task_description or active_request.task_title,
                blockers_found=[],
                validation_notes=[
                    "Execution budget (max_steps) exceeded. "
                    "Validator should assess partial completion and determine if follow-up work is needed."
                ],
                execution_agent_sequence=list(executed_subagents),
                evidence=resolution_state.evidence,
            ),
            active_request,
        )

    def _decide_next_action(
        self,
        *,
        request: ExecutionRequest,
        runtime_state: ExecutionState,
        resolution_state: ResolutionState,
    ) -> NextActionDecision:
        user_prompt = _build_orchestrator_prompt(request, runtime_state, resolution_state)

        last_exc: ValidationError | None = None
        for attempt in range(2):
            raw = self.runtime.generate_structured(
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="execution_engine_next_action",
                json_schema=NextActionDecision.model_json_schema(),
            )
            try:
                return NextActionDecision.model_validate(raw)
            except ValidationError as exc:
                last_exc = exc
                logger.warning(
                    "execution_orchestrator_invalid_decision task_id=%s attempt=%s raw=%s errors=%s",
                    request.task_id,
                    attempt + 1,
                    raw,
                    str(exc),
                )

        raise ExecutionEngineRejectedError(
            message="Execution orchestrator produced invalid next-action output.",
            rejection_reason=str(last_exc),
            remaining_scope=request.task_description or request.task_title,
            blockers_found=["invalid_next_action_output"],
            validation_notes=["The orchestrator returned invalid structured output."],
            failure_code="invalid_next_action_output",
        ) from last_exc

    @staticmethod
    def _build_step_from_decision(
        decision: NextActionDecision,
        *,
        sequence_no: int,
    ) -> ExecutionStep:
        if decision.decision_type != DECISION_CALL_SUBAGENT or not decision.subagent_name:
            raise ValueError("ExecutionStep can only be built from a call_subagent decision.")

        return ExecutionStep(
            id=f"dynamic_call_{sequence_no}_{decision.subagent_name}",
            subagent_name=decision.subagent_name,
            title=decision.subagent_name,
            instructions=decision.rationale,
            target_paths=decision.target_paths,
            metadata={},
        )
