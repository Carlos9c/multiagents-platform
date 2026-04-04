from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.execution_engine.agent_runtime import BaseAgentRuntime
from app.execution_engine.base import ExecutionEngineRejectedError
from app.execution_engine.budget import LoopBudget
from app.execution_engine.capabilities import render_executor_capabilities_for_prompt
from app.execution_engine.contracts import (
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_PARTIAL,
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


ORCHESTRATOR_SYSTEM_PROMPT = """
You are the execution orchestrator for one already-atomic task.

Your job is to decide exactly one of these three things:
1. call_subagent -> because there is a clear and safe next subagent contribution
2. finish -> because there is no clearly better next contribution remaining
3. reject -> because there is no safe operational contribution possible with the available subagents and tools

You do not perform the work yourself.
You coordinate subagents.
You must never change the task itself.

Return ONLY JSON matching the provided schema.

Core responsibility:
- Look at the task, the current phase, the accumulated evidence, and the last executed subagent.
- Decide the single next best orchestration decision.
- Keep the execution ordered, purposeful, and coherent.
- Continue while a subagent can still provide meaningful operational progress.
- Finish when no better next contribution remains.
- Reject only when no safe operational route exists with the current execution engine.

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
   - Call context_selection_agent if execution reveals a real context gap.
   - Call code_change_agent if repository changes are needed.
   - Call command_runner_agent if repository-local verification would materially improve the evidence.
   - Finish only when no subagent has a clearly better next contribution.
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
- Use command_runner_agent only when there is already a meaningful candidate to verify.
- Do not use command_runner_agent for exploration.

How to judge whether the operational pass is sufficient:
- Your goal is not to prove the task is perfect.
- Your goal is to coordinate subagents until no clearly better next operational step remains.
- Treat the current process as sufficient when:
  - the execution context is already adequate,
  - the needed repository changes have already been materialized if the task required them,
  - repository-local verification has already been performed if it would materially improve confidence,
  - and calling another subagent would likely be redundant, speculative, or lower-value than finishing now.

What to inspect before choosing finish:
- Look at accumulated evidence_items.
- Look at changed_files to see whether implementation work already happened.
- Look at executed_commands to see whether operational verification already happened.
- Look at the last completed subagent to avoid repeating work.
- Look at risk_flags and failed_steps to understand whether another subagent could still resolve a concrete gap.

Do not finish just because some work exists.
Finish because there is no clearly better next subagent contribution.

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


def _last_completed_subagent_name(resolution_state: ResolutionState) -> str | None:
    if not resolution_state.completed_steps:
        return None

    last_step_id = resolution_state.completed_steps[-1]
    prefix = "dynamic_call_"
    if last_step_id.startswith(prefix):
        return last_step_id[len(prefix) :]
    return None


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


def _build_operational_state_summary(resolution_state: ResolutionState) -> dict:
    last_subagent = _last_completed_subagent_name(resolution_state)

    return {
        "phase": resolution_state.phase,
        "has_historical_context": resolution_state.historical_task_selection is not None,
        "has_changed_files": bool(resolution_state.evidence.changed_files),
        "has_command_evidence": bool(resolution_state.evidence.commands),
        "has_any_outputs": resolution_state.has_outputs(),
        "last_completed_subagent_name": last_subagent,
        "completed_step_count": len(resolution_state.completed_steps),
        "failed_step_count": len(resolution_state.failed_steps),
        "changed_file_count": len(resolution_state.evidence.changed_files),
        "command_count": len(resolution_state.evidence.commands),
        "artifact_count": len(resolution_state.evidence.artifacts_created),
        "risk_flag_count": len(resolution_state.risk_flags),
        "has_completed_any_step": bool(resolution_state.completed_steps),
        "has_failed_any_step": bool(resolution_state.failed_steps),
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
    last_subagent = _last_completed_subagent_name(resolution_state)

    return f"""
Task:
- task_id: {request.task_id}
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
- tool_call_count: {runtime_state.tool_call_count}
- command_run_count: {runtime_state.command_run_count}
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
- last_completed_subagent_name: {last_subagent}
- risk_flags: {resolution_state.risk_flags}
- step_notes: {resolution_state.step_notes}
- operational_state_summary: {_build_operational_state_summary(resolution_state)}

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
- You must not call the same subagent as the last completed subagent.
- In discovery, the task is still preparing context.
- In execution, decide which subagent can still provide the best next operational progress.
- Finish if no better clear contribution remains.
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
    del request, runtime_state

    last_subagent = _last_completed_subagent_name(state)
    allowed_subagents = _allowed_subagents_for_phase(state.phase)

    if not state.completed_steps:
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

        if last_subagent is not None and decision.subagent_name == last_subagent:
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

    return decision


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

    def run(self, db: Session, request: ExecutionRequest) -> ExecutionResult:
        runtime_state = ExecutionState()
        resolution_state = ResolutionState(
            execution_request=request,
            orchestrator_trace=OrchestratorTrace(task_id=request.task_id),
        )
        executed_subagents: list[str] = []

        active_request = resolution_state.execution_request

        resolution_state.orchestrator_trace.add_event(
            event_type="orchestrator_started",
            step_count=runtime_state.step_count,
            task_id=active_request.task_id,
            payload={
                "title": active_request.task_title,
                "executor_type": active_request.executor_type,
                "max_steps": self.budget.max_steps,
                "registered_subagents": self.registry.all_names(),
            },
        )

        logger.info(
            "execution_orchestrator_started task_id=%s executor_type=%s max_steps=%s",
            active_request.task_id,
            active_request.executor_type,
            self.budget.max_steps,
        )

        while runtime_state.step_count < self.budget.max_steps:
            active_request = resolution_state.execution_request

            raw_decision = self._decide_next_action(
                request=active_request,
                runtime_state=runtime_state,
                resolution_state=resolution_state,
            )
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
                    "execution_orchestrator_decision_normalized task_id=%s phase=%s original=%s/%s normalized=%s/%s",
                    active_request.task_id,
                    resolution_state.phase,
                    raw_decision.decision_type,
                    raw_decision.subagent_name,
                    decision.decision_type,
                    decision.subagent_name,
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
                    },
                )

            resolution_state.orchestrator_trace.add_event(
                event_type="next_action_decided",
                step_count=runtime_state.step_count,
                task_id=active_request.task_id,
                payload=decision.model_dump(),
            )
            runtime_state.register_step()

            if decision.decision_type == DECISION_FINISH:
                remaining_scope = active_request.task_description or active_request.task_title
                blockers_found = list(resolution_state.risk_flags)

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
                    "execution_orchestrator_finished task_id=%s changed_files=%s commands=%s",
                    active_request.task_id,
                    len(resolution_state.evidence.changed_files),
                    len(resolution_state.evidence.commands),
                )

                return ExecutionResult(
                    task_id=active_request.task_id,
                    decision=EXECUTION_DECISION_PARTIAL,
                    summary="Operational execution loop completed successfully.",
                    details=decision.rationale,
                    completed_scope="Execution engine completed its current operational pass.",
                    remaining_scope=remaining_scope,
                    blockers_found=blockers_found,
                    validation_notes=[
                        "Execution orchestrator finished normally.",
                        *resolution_state.risk_flags,
                    ],
                    execution_agent_sequence=list(executed_subagents),
                    evidence=resolution_state.evidence,
                )

            if decision.decision_type == DECISION_INVALID:
                runtime_state.register_agent_call("execution_orchestrator_invalid_decision")
                resolution_state.mark_step_failed("orchestrator_invalid_decision")
                resolution_state.add_note(
                    "The orchestrator proposed an invalid decision for the current state and must decide again."
                )
                resolution_state.add_risk_flags(decision.risk_flags)
                resolution_state.orchestrator_trace.add_event(
                    event_type="orchestrator_decision_invalidated",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload=decision.model_dump(),
                )
                resolution_state.evidence.add_note(
                    message=(
                        "Orchestrator decision was invalid for the current state. "
                        "The loop will continue and consume budget."
                    ),
                    producer="execution_orchestrator",
                )
                continue

            if decision.decision_type == DECISION_REJECT:
                remaining_scope = active_request.task_description or active_request.task_title
                blockers_found = list(decision.risk_flags)

                resolution_state.orchestrator_trace.add_event(
                    event_type="orchestrator_rejected",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload=decision.model_dump(),
                )
                _append_trace_notes_to_evidence(resolution_state)

                return ExecutionResult(
                    task_id=active_request.task_id,
                    decision=EXECUTION_DECISION_FAILED,
                    summary="Execution orchestrator rejected the task.",
                    details=decision.rationale,
                    remaining_scope=remaining_scope,
                    blockers_found=blockers_found,
                    validation_notes=["Execution orchestrator rejected the task."],
                    execution_agent_sequence=list(executed_subagents),
                    evidence=resolution_state.evidence,
                )

            try:
                step = self._build_step_from_decision(decision)

                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_selected",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "subagent_name": step.subagent_name,
                        "target_paths": step.target_paths,
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
                resolution_state.mark_step_failed(decision.subagent_name or decision.decision_type)
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_registry_error",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={"error": str(exc)},
                )
                _append_trace_notes_to_evidence(resolution_state)
                return ExecutionResult(
                    task_id=active_request.task_id,
                    decision=EXECUTION_DECISION_FAILED,
                    summary=str(exc),
                    details="The orchestrator selected an unregistered subagent.",
                    remaining_scope=active_request.task_description or active_request.task_title,
                    blockers_found=[str(exc)],
                    validation_notes=["Registry misconfiguration in orchestrator loop."],
                    execution_agent_sequence=list(executed_subagents),
                    evidence=resolution_state.evidence,
                )

            except SubagentRejectedStepError as exc:
                resolution_state.mark_step_failed(decision.subagent_name or decision.decision_type)
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
                resolution_state.mark_step_failed(decision.subagent_name or decision.decision_type)
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
                return ExecutionResult(
                    task_id=active_request.task_id,
                    decision=EXECUTION_DECISION_FAILED,
                    summary=f"Unexpected orchestrator loop failure: {str(exc)}",
                    details="Unexpected exception inside execution orchestrator loop.",
                    remaining_scope=active_request.task_description or active_request.task_title,
                    blockers_found=[str(exc)],
                    validation_notes=["Unexpected orchestrator loop exception."],
                    execution_agent_sequence=list(executed_subagents),
                    evidence=resolution_state.evidence,
                )

        active_request = resolution_state.execution_request

        resolution_state.orchestrator_trace.add_event(
            event_type="orchestrator_budget_exceeded",
            step_count=runtime_state.step_count,
            task_id=active_request.task_id,
            payload={"max_steps": self.budget.max_steps},
        )
        _append_trace_notes_to_evidence(resolution_state)

        return ExecutionResult(
            task_id=active_request.task_id,
            decision=EXECUTION_DECISION_FAILED,
            summary="Execution budget exceeded before a valid finish decision.",
            details="The orchestrator loop exceeded max_steps.",
            remaining_scope=active_request.task_description or active_request.task_title,
            blockers_found=["max_steps exceeded"],
            validation_notes=["Execution orchestrator exceeded its budget."],
            execution_agent_sequence=list(executed_subagents),
            evidence=resolution_state.evidence,
        )

    def _decide_next_action(
        self,
        *,
        request: ExecutionRequest,
        runtime_state: ExecutionState,
        resolution_state: ResolutionState,
    ) -> NextActionDecision:
        schema = to_openai_strict_json_schema(NextActionDecision.model_json_schema())
        raw = self.runtime.generate_structured(
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
            user_prompt=_build_orchestrator_prompt(
                request,
                runtime_state,
                resolution_state,
            ),
            schema_name="execution_engine_next_action",
            json_schema=schema,
        )

        try:
            return NextActionDecision.model_validate(raw)
        except ValidationError as exc:
            raise ExecutionEngineRejectedError(
                message="Execution orchestrator produced invalid next-action output.",
                rejection_reason=str(exc),
                remaining_scope=request.task_description or request.task_title,
                blockers_found=["invalid_next_action_output"],
                validation_notes=["The orchestrator returned invalid structured output."],
                failure_code="invalid_next_action_output",
            ) from exc

    @staticmethod
    def _build_step_from_decision(decision: NextActionDecision) -> ExecutionStep:
        if decision.decision_type != DECISION_CALL_SUBAGENT or not decision.subagent_name:
            raise ValueError("ExecutionStep can only be built from a call_subagent decision.")

        return ExecutionStep(
            id=f"dynamic_call_{decision.subagent_name}",
            subagent_name=decision.subagent_name,
            title=decision.subagent_name,
            instructions=decision.rationale,
            target_paths=decision.target_paths,
            metadata={},
        )
