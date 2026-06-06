from __future__ import annotations

import logging

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.execution_plan import ExecutionPlan, ExecutionPlanGenerationInput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_planning_trace

_logger = logging.getLogger(__name__)

EXECUTION_SEQUENCER_SYSTEM_PROMPT = prompt_loader.get("execution_sequencer")


def build_execution_sequencer_user_prompt(
    sequencing_input: ExecutionPlanGenerationInput,
) -> str:
    prompt_loader.validate_builder_inputs(
        "execution_sequencer",
        "main",
        {
            "sequencing_input": sequencing_input,
        },
    )
    return f"""
Generate an execution plan for the following project execution context.

You must return valid JSON matching the schema.

Execution sequencing input:
{sequencing_input.model_dump_json(indent=2)}
""".strip()


def build_execution_sequencer_retry_prompt(
    sequencing_input: ExecutionPlanGenerationInput,
    validation_error: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "execution_sequencer",
        "retry",
        {
            "sequencing_input": sequencing_input,
            "validation_error": validation_error,
        },
    )
    return f"""
Generate an execution plan for the following project execution context.

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only valid JSON
- execution_batches must not be empty
- every batch must have at least one task
- every batch must have checkpoint_after=true
- every batch must define checkpoint_id and checkpoint_reason
- every batch checkpoint_id must reference a real checkpoint definition
- do not invent task IDs outside the provided candidates
- checkpoint references must align with actual batches
- ready_task_ids should only include tasks safe to start now
- blocked_task_ids should include tasks waiting on inferred prerequisites
- inferred_dependencies must be meaningful and justified
- explicitly include uncertainties where dependency inference is not fully certain
- the final checkpoint must include "stage_closure" in evaluation_focus
- tasks with ordering_hint="setup_first" MUST appear in earlier batches than tasks with ordering_hint="depends_on_setup"
- HARD CONSTRAINT: every task whose depends_on_task_titles lists another task MUST be placed in a STRICTLY LATER batch_index than each of those prerequisites

Execution sequencing input:
{sequencing_input.model_dump_json(indent=2)}
""".strip()


def _sort_execution_batches_by_index(raw: dict) -> dict:
    """Pre-sort execution_batches by batch_index before Pydantic validation.

    The Pydantic validator requires the list to be in non-decreasing batch_index
    order, but the LLM may return batches in a different sequence.  Sorting is
    safe because _normalize_execution_plan() in execution_plan_service.py
    overwrites every batch_index with a sequential position value anyway.
    """
    if not isinstance(raw, dict):
        return raw
    execution_batches = raw.get("execution_batches")
    if not isinstance(execution_batches, list) or len(execution_batches) < 2:
        return raw

    try:
        raw["execution_batches"] = sorted(
            execution_batches,
            key=lambda b: int(b.get("batch_index", 0)) if isinstance(b, dict) else 0,
        )
    except (TypeError, ValueError):
        pass  # Non-sortable data — let model_validate surface the real error

    return raw


def _ensure_final_checkpoint_stage_closure(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return raw

    execution_batches = raw.get("execution_batches")
    checkpoints = raw.get("checkpoints")

    if not isinstance(execution_batches, list) or not execution_batches:
        return raw
    if not isinstance(checkpoints, list) or not checkpoints:
        return raw

    final_batch = execution_batches[-1]
    if not isinstance(final_batch, dict):
        return raw

    # Mirror the model validator's lookup: find the checkpoint whose checkpoint_id
    # matches the final batch's checkpoint_id (not by after_batch_id).
    final_checkpoint_id = final_batch.get("checkpoint_id")
    if not final_checkpoint_id:
        return raw

    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict):
            continue
        if checkpoint.get("checkpoint_id") != final_checkpoint_id:
            continue

        focus = checkpoint.get("evaluation_focus")
        if not isinstance(focus, list):
            focus = []

        normalized_focus = [str(item) for item in focus if item]
        if "stage_closure" not in normalized_focus:
            normalized_focus.append("stage_closure")

        checkpoint["evaluation_focus"] = normalized_focus
        break

    return raw


def _validate_dependency_ordering(
    plan: ExecutionPlan,
    sequencing_input: ExecutionPlanGenerationInput,
) -> list[str]:
    """Verify that all depends_on_task_titles constraints are respected in the plan.

    For every candidate task T with depends_on_task_titles=[P, ...], T's batch_index
    must be strictly greater than the batch_index of each prerequisite P.

    Returns a list of human-readable violation strings.  Empty list means no violations.
    """
    # title → task_id lookup (candidates only)
    id_by_title: dict[str, int] = {
        t.title: t.task_id for t in sequencing_input.candidate_atomic_tasks
    }
    # task_id → batch_index from the produced plan
    batch_index_by_task: dict[int, int] = {}
    for batch in plan.execution_batches:
        for task_id in batch.task_ids:
            batch_index_by_task[task_id] = batch.batch_index

    violations: list[str] = []
    for task in sequencing_input.candidate_atomic_tasks:
        if not task.depends_on_task_titles:
            continue
        task_batch = batch_index_by_task.get(task.task_id)
        if task_batch is None:
            # Task is blocked / not scheduled — no ordering constraint to check
            continue
        for dep_title in task.depends_on_task_titles:
            dep_id = id_by_title.get(dep_title)
            if dep_id is None:
                continue  # title not found among candidates — silently skip
            dep_batch = batch_index_by_task.get(dep_id)
            if dep_batch is None:
                continue  # prerequisite not scheduled — skip
            if task_batch <= dep_batch:
                violations.append(
                    f"Task '{task.title}' (batch {task_batch}) declares "
                    f"'{dep_title}' as a prerequisite (batch {dep_batch}), "
                    f"but must be in a STRICTLY LATER batch."
                )
    return violations


def _format_dependency_violations(violations: list[str]) -> str:
    lines = ["depends_on_task_titles ordering constraint violations:"]
    for v in violations:
        lines.append(f"  - {v}")
    lines.append(
        "\nTo fix: ensure every task whose depends_on_task_titles lists another task "
        "is placed in a batch_index strictly higher than each of those prerequisites. "
        "Placing them in the same batch is also a violation."
    )
    return "\n".join(lines)


def _append_sequencer_trace(
    *,
    project_id: int | None,
    call_type: str,
    sequencing_input: ExecutionPlanGenerationInput,
    result: ExecutionPlan,
) -> None:
    if project_id is None:
        return
    append_planning_trace(
        project_id=project_id,
        entry={
            "agent": "execution_sequencer",
            "call_type": call_type,
            "project_id": project_id,
            "plan_version": result.plan_version,
            "supersedes_plan_version": result.supersedes_plan_version,
            "batches_produced_count": len(result.execution_batches),
            "batch_sizes": [len(b.task_ids) for b in result.execution_batches],
            "input_snapshot": sequencing_input.model_dump(),
            "output_snapshot": result.model_dump(),
        },
    )


def call_execution_sequencer_model(
    sequencing_input: ExecutionPlanGenerationInput,
    *,
    project_id: int | None = None,
    call_type: str = "initial",
) -> ExecutionPlan:
    provider = get_llm_provider(
        model=settings.sequencer_model,
        provider=settings.sequencer_provider,
    )
    first_user_prompt = build_execution_sequencer_user_prompt(sequencing_input)

    raw = provider.generate_structured(
        system_prompt=EXECUTION_SEQUENCER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="execution_plan",
        json_schema=ExecutionPlan.model_json_schema(),
    )
    raw = _ensure_final_checkpoint_stage_closure(raw)
    raw = _sort_execution_batches_by_index(raw)

    validation_error: str | None = None
    try:
        result = ExecutionPlan.model_validate(raw)
    except ValidationError as exc:
        validation_error = str(exc)
    else:
        violations = _validate_dependency_ordering(result, sequencing_input)
        if violations:
            validation_error = _format_dependency_violations(violations)

    if validation_error is not None:
        retry_user_prompt = build_execution_sequencer_retry_prompt(
            sequencing_input=sequencing_input,
            validation_error=validation_error,
        )
        raw_retry = provider.generate_structured(
            system_prompt=EXECUTION_SEQUENCER_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="execution_plan",
            json_schema=ExecutionPlan.model_json_schema(),
        )
        raw_retry = _ensure_final_checkpoint_stage_closure(raw_retry)
        raw_retry = _sort_execution_batches_by_index(raw_retry)
        result = ExecutionPlan.model_validate(raw_retry)

        retry_violations = _validate_dependency_ordering(result, sequencing_input)
        if retry_violations:
            _logger.warning(
                "depends_on_task_titles violations remain after retry (project_id=%s): %s",
                project_id,
                "; ".join(retry_violations),
            )

    _append_sequencer_trace(
        project_id=project_id,
        call_type=call_type,
        sequencing_input=sequencing_input,
        result=result,
    )
    return result
