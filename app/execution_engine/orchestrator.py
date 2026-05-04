from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.base import ExecutionEngineRejectedError, ExecutionEngineTransientError
from app.execution_engine.budget import LoopBudget
from app.execution_engine.capabilities import render_executor_capabilities_for_prompt
from app.execution_engine.contracts import (
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_FAILED,
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
from app.services.llm.schema_utils import to_openai_strict_json_schema

logger = logging.getLogger(__name__)


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


ORCHESTRATOR_SYSTEM_PROMPT = """
You are the execution orchestrator for one already-atomic task.

Your job is to decide exactly one of these three things:
1. call_subagent -> because there is a clear and safe next subagent contribution
2. finish -> because the operational pass is sufficient and no clearly better next contribution remains
3. reject -> because there is no safe operational contribution possible with the available subagents and tools

You do not perform the work yourself.
You coordinate subagents.
You must never change the task itself.

Return ONLY JSON matching the provided schema.

JSON field rules (enforced by the validator — violations cause the whole run to fail):
- When decision_type is "finish", "reject", or "invalid": subagent_name MUST be null and target_paths MUST be an empty list.
- When decision_type is "call_subagent": subagent_name MUST be a non-null string from {context_selection_agent, code_change_agent, command_runner_agent}.

Core responsibility:
- Look at the task, the current phase, the accumulated evidence, and the last executed subagent.
- Decide the single next best orchestration decision.
- Keep the execution ordered, purposeful, and coherent.
- Prefer the minimum next useful progress.
- Finish as soon as the operational pass is sufficient.
- Reject only when no safe operational route exists with the current execution engine.

Critical boundary:
- You are NOT a quality reviewer of subagent outputs.
- You do NOT judge whether produced files are elegant, ideal, over-designed, or minimally perfect.
- You do NOT request another subagent call just to polish or improve an already sufficient output.
- You only decide whether another operational step is still required to move the task forward.
- Risk notes and warnings are not, by themselves, concrete gaps.
- A concrete gap exists only when a specific next subagent action is still operationally necessary.

Phase model:
1. discovery
   Meaning:
   - The task is still preparing the context needed for correct execution.
   - If no subagent has acted yet, you must start by calling context_selection_agent.

   Normal behavior:
   - Call context_selection_agent.
   - Do not finish from discovery.
   - Do not call implementation or verification subagents before context has been selected.

2. execution
   Meaning:
   - The task is in active operational execution.
   - From here you may call subagents, finish, or reject.

   Normal behavior:
   - Call context_selection_agent only if execution reveals a real and hard context gap.
   - Call code_change_agent only if repository changes or material file edits are still needed, or if the last verification attempt failed in a way that repository changes can repair.
   - Call command_runner_agent only if repository-local verification would materially improve the evidence.
   - Finish when the completion checklist says the pass is sufficient.
   - Reject only when no safe contribution is possible with the available subagents.

Subagent sequencing rule:
- You must not call the same subagent that was executed most recently.
- If a subagent just acted, the next valid decision must either:
  - call a different subagent, or
  - finish, or
  - reject.

How to choose subagents:
- Choose the subagent whose role best matches the current need.
- Prefer the minimum next useful progress.
- Do not call a subagent just because it exists.
- Use the accumulated evidence to decide what is still missing.
- Use context_selection_agent again only when a genuine context gap appears during execution.
- Do not use command_runner_agent for exploration.
- Repository-local verification is not automatically required just because files changed.
- For documentation, requirements, specification, README, or design-note tasks, repository-local command execution is often not materially useful unless the task explicitly asks for a check, test, lint, build, or other executable verification step.
- A failed verification attempt does not by itself prove that the task still has a concrete product gap.
- If verification later succeeds and covers the task materially, do not continue the loop just because earlier attempts failed.

Binary completion checklist:
You must explicitly reason over these four fields before deciding:
- context_ready: yes/no
- implementation_done_if_needed: yes/no
- local_verification_done_if_material: yes/no
- new_concrete_gap_detected: yes/no

Interpretation:
- context_ready = yes when the execution context is already sufficient for the current task.
- implementation_done_if_needed = yes when required repository changes have already been materialized, or when no material repository change is needed for this task.
- local_verification_done_if_material = yes when repository-local verification has already been performed when it would materially improve confidence, or when such verification would not materially improve the evidence.
- new_concrete_gap_detected = yes only when there is a specific, operationally actionable missing piece that another subagent can address now.
- Warnings, smells, design preferences, and speculative concerns do not count as new concrete gaps unless they block the task objective or the required verification.

Hard finish rule:
- If context_ready=yes
- and implementation_done_if_needed=yes
- and local_verification_done_if_material=yes
- and new_concrete_gap_detected=no
- then you must choose finish.

What to inspect before choosing finish:
- Look at accumulated evidence_items.
- Look at changed_files to see whether implementation work already happened.
- Look at executed_commands to see whether operational verification already happened.
- Look at the last attempted subagent to avoid repeating work.
- Look at whether the latest command evidence succeeded or failed.
- Do not keep the loop open merely because some warning or risk note exists.

Do not finish just because some work exists.
Finish because the checklist says the current operational pass is sufficient.

How to use reject:
- Reject is a last-resort orchestration decision.
- Choose reject only when no available subagent can safely provide meaningful next progress.
- Reject means there is no safe and meaningful operational route left inside this execution engine.

Reject is correct when:
- the task requires work outside the capabilities of all available subagents,
- the task cannot be advanced through context selection, repository changes, or repository-local verification,
- the current state is structurally compatible with the engine, but operationally no safe next step exists.

Do not reject when:
- you simply chose the wrong subagent in the previous iteration,
- changes could still advance the task,
- local verification could still improve the evidence,
- the task is incomplete but still operationally actionable.

Hard constraints:
- Never change the task.
- Decide exactly one orchestration decision.
- Use only real subagents available in the system.
- Do not invent hidden tools or hidden execution paths.
- Do not call the same subagent twice in a row.
- If no subagent has acted yet, call context_selection_agent.
""".strip()


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


def _verification_would_materially_improve(
    request: ExecutionRequest,
    resolution_state: ResolutionState,
) -> bool:
    latest_command = _latest_command_execution(resolution_state)
    if latest_command is not None and _command_succeeded(latest_command):
        return False

    if _is_documentation_like_task(request):
        if _task_explicitly_requests_repo_local_verification(request):
            return latest_command is None

        if _changed_files_look_documentation_only(resolution_state):
            return False

        if resolution_state.evidence.changed_files:
            return False

    if _task_explicitly_requests_repo_local_verification(request):
        return latest_command is None

    if resolution_state.evidence.changed_files and _is_executable_implementation_like_task(request):
        return latest_command is None

    return False


def _failed_verification_is_repairable_by_repo_changes(
    *,
    request: ExecutionRequest,
    resolution_state: ResolutionState,
    command,
) -> bool:
    if command is None:
        return False

    if _command_succeeded(command):
        return False

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

    if last_attempted_subagent == "code_change_agent":
        return True

    if last_attempted_subagent == "command_runner_agent":
        latest_command = _latest_command_execution(resolution_state)

        if latest_command is None:
            return False

        if _command_succeeded(latest_command):
            return False

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
        # code_change_agent completing a step is direct evidence that the
        # implementation pass ran, even when keyword detection is ambiguous
        or _code_change_agent_completed_a_step(resolution_state)
    )

    verification_needed = _verification_would_materially_improve(request, resolution_state)
    latest_command = _latest_command_execution(resolution_state)
    local_verification_done_if_material = not verification_needed or (
        latest_command is not None and _command_succeeded(latest_command)
    )

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
- For implementation, testing, or configuration task types, do not finish unless at least one of code_change_agent or command_runner_agent has executed.
- A checklist that looks satisfied but contradicts the task_type or the visible evidence means a subagent step was likely skipped — call the appropriate subagent instead of finishing.

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
            return _invalidate_decision(
                decision,
                rationale=(f"Subagent '{decision.subagent_name}' cannot be called twice in a row."),
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

    if (
        last_attempted_subagent == "code_change_agent"
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
        last_attempted_subagent == "code_change_agent"
        and not resolution_state.evidence.changed_files
        and not resolution_state.failed_steps
        and resolution_state.completed_steps
    ):
        return NextActionDecision(
            decision_type=DECISION_FINISH,
            rationale=(
                "The last implementation pass completed without materialized file changes and no concrete "
                "operational gap remains, so the orchestration should stop instead of looping."
            ),
            expected_outcome="Stop orchestration because another immediate implementation pass would be redundant.",
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

        return (
            ExecutionResult(
                task_id=active_request.task_id,
                decision=EXECUTION_DECISION_FAILED,
                summary="Execution budget exceeded before a valid finish decision.",
                details="The orchestrator loop exceeded max_steps.",
                remaining_scope=active_request.task_description or active_request.task_title,
                blockers_found=["max_steps exceeded"],
                validation_notes=["Execution orchestrator exceeded its budget."],
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
        schema = to_openai_strict_json_schema(NextActionDecision.model_json_schema())
        user_prompt = _build_orchestrator_prompt(request, runtime_state, resolution_state)

        last_exc: ValidationError | None = None
        for attempt in range(2):
            raw = self.runtime.generate_structured(
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="execution_engine_next_action",
                json_schema=schema,
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
