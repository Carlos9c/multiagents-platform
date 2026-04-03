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
    ACTION_APPLY_FILE_OPERATIONS,
    ACTION_FINISH,
    ACTION_INSPECT_CONTEXT,
    ACTION_REJECT,
    ACTION_RUN_COMMAND,
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

Your responsibility is to decide the next best operational action.
You must not modify the task itself.

Return ONLY JSON matching the provided schema.

Hard rules:
- Never change the task.
- Prefer the minimum next useful action.
- Use inspect_context when more context preparation is needed before execution.
- Use apply_file_operations when implementation should begin.
- Use run_command only when a concrete command is actually necessary.
- run_command is for one narrow concrete command only, not shell scripting.
- Do not use run_command for open-ended exploration.
- Do not use shell chaining, pipes, redirection, or multi-command sequences.
- Prefer finish over run_command unless the command has a clear and immediate purpose.
- Use finish when the current operational pass is sufficient for handing off to external validation.
- Use reject only when no safe operational route exists.
- Risk flags should inform caution, not automatically block progress.
- You must reason from the ACTUAL subagents and tools listed in the prompt.
- Do not invent capabilities, subagents, tools, or hidden execution paths.
- Do not keep retrying the same class of action just because it already failed once; use the current state and evidence.
""".strip()


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

Orchestrator action routing:
- inspect_context -> context_selection_agent
- apply_file_operations -> code_change_agent
- run_command -> command_runner_agent
- finish -> no subagent; return control to external validation
- reject -> no subagent; reject execution

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

Current resolution state:
- phase: {resolution_state.phase}
- historical_task_selection_present: {resolution_state.historical_task_selection is not None}
- materialization_attempt_count: {resolution_state.materialization_attempt_count}
- changed_files: {[item.model_dump() for item in resolution_state.evidence.changed_files]}
- executed_commands: {[item.model_dump() for item in resolution_state.evidence.commands]}
- files_read: {resolution_state.evidence.files_read}
- risk_flags: {resolution_state.risk_flags}
- step_notes: {resolution_state.step_notes}
- evidence_notes: {resolution_state.evidence.notes}

Decision discipline:
- Select exactly one next action.
- Prefer progress over premature blocking.
- Respect phase policy and current state.
- Completion phase should normally finish unless a concrete command is truly necessary.
- Avoid recursive behavior such as repeatedly choosing the same class of action without new evidence.
- A run_command decision must contain exactly one concrete command with a narrow purpose.
- Do not use run_command to compensate for missing context preparation.
""".strip()


def _allowed_actions(
    request: ExecutionRequest,
    state: ResolutionState,
    runtime_state: ExecutionState,
) -> list[str]:
    if state.phase == "discovery":
        return [ACTION_INSPECT_CONTEXT, ACTION_REJECT]

    if state.phase == "execution":
        return [ACTION_APPLY_FILE_OPERATIONS, ACTION_REJECT]

    if state.phase == "completion":
        if runtime_state.command_run_count == 0:
            return [ACTION_FINISH, ACTION_RUN_COMMAND, ACTION_REJECT]
        return [ACTION_FINISH, ACTION_REJECT]

    return [ACTION_REJECT]


def _normalize_decision(
    request: ExecutionRequest,
    state: ResolutionState,
    runtime_state: ExecutionState,
    decision: NextActionDecision,
) -> NextActionDecision:
    allowed = _allowed_actions(request, state, runtime_state)

    if decision.action == ACTION_RUN_COMMAND and not (
        decision.command and decision.command.strip()
    ):
        return NextActionDecision(
            action=ACTION_FINISH,
            rationale=(
                "Completion phase received run_command without a concrete command. "
                "Finish the operational pass instead."
            ),
            target_paths=[],
            command=None,
            expected_outcome="Hand off to external validation.",
            risk_flags=list(decision.risk_flags)
            + ["run_command_missing_command_overridden_to_finish"],
        )

    if (
        state.phase == "completion"
        and decision.action == ACTION_RUN_COMMAND
        and runtime_state.command_run_count >= 1
    ):
        return NextActionDecision(
            action=ACTION_FINISH,
            rationale=(
                "A completion-phase command was already attempted. "
                "Avoid open-ended command looping and finish the operational pass."
            ),
            target_paths=[],
            command=None,
            expected_outcome="Hand off to external validation.",
            risk_flags=list(decision.risk_flags)
            + ["completion_run_command_capped_overridden_to_finish"],
        )

    if decision.action in allowed:
        return decision

    fallback_action = allowed[0]

    rationale_map = {
        ACTION_INSPECT_CONTEXT: "Current phase requires context preparation first.",
        ACTION_APPLY_FILE_OPERATIONS: "Current phase requires implementation to begin.",
        ACTION_RUN_COMMAND: "Current phase allows one concrete command execution before completion.",
        ACTION_FINISH: "Current phase should finish the operational pass.",
        ACTION_REJECT: "No safe operational route is available in the current phase.",
    }

    return NextActionDecision(
        action=fallback_action,
        rationale=rationale_map[fallback_action],
        target_paths=[],
        command=None,
        expected_outcome=None,
        risk_flags=list(decision.risk_flags) + ["action_overridden_by_phase_policy"],
    )


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

            if decision.action != raw_decision.action or decision.command != raw_decision.command:
                logger.warning(
                    "execution_orchestrator_action_overridden task_id=%s phase=%s original=%s normalized=%s",
                    active_request.task_id,
                    resolution_state.phase,
                    raw_decision.action,
                    decision.action,
                )
                resolution_state.orchestrator_trace.add_event(
                    event_type="action_overridden_by_phase_policy",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "phase": resolution_state.phase,
                        "original_action": raw_decision.action,
                        "normalized_action": decision.action,
                        "original_command": raw_decision.command,
                        "normalized_command": decision.command,
                    },
                )

            resolution_state.orchestrator_trace.add_event(
                event_type="next_action_decided",
                step_count=runtime_state.step_count,
                task_id=active_request.task_id,
                payload=decision.model_dump(),
            )
            runtime_state.register_step()

            if decision.action == ACTION_FINISH:
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
                resolution_state.evidence.notes.extend(
                    resolution_state.orchestrator_trace.to_notes()
                )

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

            if decision.action == ACTION_REJECT:
                resolution_state.orchestrator_trace.add_event(
                    event_type="orchestrator_rejected",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload=decision.model_dump(),
                )
                raise ExecutionEngineRejectedError(
                    message="Execution orchestrator could not find a safe operational route.",
                    rejection_reason=decision.rationale,
                    remaining_scope=active_request.task_description or active_request.task_title,
                    blockers_found=decision.risk_flags,
                    validation_notes=["Execution orchestrator rejected the task."],
                    failure_code="orchestrator_rejected",
                )

            try:
                step = self._build_step_from_decision(decision)

                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_selected",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={
                        "subagent_name": step.subagent_name,
                        "kind": step.kind,
                        "target_paths": step.target_paths,
                        "command": step.command,
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

                if decision.action == ACTION_RUN_COMMAND:
                    runtime_state.register_command_run()

                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_completed",
                    step_count=runtime_state.step_count,
                    task_id=resolution_state.execution_request.task_id,
                    payload={
                        "subagent_name": step.subagent_name,
                        "kind": step.kind,
                        "phase": resolution_state.phase,
                        "changed_files_count": len(resolution_state.evidence.changed_files),
                        "commands_count": len(resolution_state.evidence.commands),
                    },
                )

            except SubagentRegistryError as exc:
                resolution_state.mark_step_failed(decision.action)
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_registry_error",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={"error": str(exc)},
                )
                resolution_state.evidence.notes.extend(
                    resolution_state.orchestrator_trace.to_notes()
                )
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
                resolution_state.mark_step_failed(decision.action)
                resolution_state.add_risk_flags([str(exc)])
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_rejected_step",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={"action": decision.action, "error": str(exc)},
                )

            except Exception as exc:
                resolution_state.mark_step_failed(decision.action)
                resolution_state.orchestrator_trace.add_event(
                    event_type="subagent_unexpected_error",
                    step_count=runtime_state.step_count,
                    task_id=active_request.task_id,
                    payload={"action": decision.action, "error": str(exc)},
                )
                resolution_state.evidence.notes.extend(
                    resolution_state.orchestrator_trace.to_notes()
                )
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
        resolution_state.evidence.notes.extend(resolution_state.orchestrator_trace.to_notes())

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
        mapping = {
            ACTION_INSPECT_CONTEXT: ("context_selection_agent", "inspect_context"),
            ACTION_APPLY_FILE_OPERATIONS: (
                "code_change_agent",
                "apply_file_operations",
            ),
            ACTION_RUN_COMMAND: ("command_runner_agent", "run_command"),
        }

        subagent_name, kind = mapping[decision.action]

        return ExecutionStep(
            id=f"dynamic_{decision.action}",
            kind=kind,
            subagent_name=subagent_name,
            title=decision.action,
            instructions=decision.rationale,
            target_paths=decision.target_paths,
            command=decision.command,
            metadata={},
        )
