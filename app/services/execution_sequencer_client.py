from __future__ import annotations

from pydantic import ValidationError

from app.schemas.execution_plan import ExecutionPlan, ExecutionPlanGenerationInput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_planning_trace

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

Execution sequencing input:
{sequencing_input.model_dump_json(indent=2)}
""".strip()


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
    provider = get_llm_provider()
    first_user_prompt = build_execution_sequencer_user_prompt(sequencing_input)

    raw = provider.generate_structured(
        system_prompt=EXECUTION_SEQUENCER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="execution_plan",
        json_schema=ExecutionPlan.model_json_schema(),
    )
    raw = _ensure_final_checkpoint_stage_closure(raw)

    try:
        result = ExecutionPlan.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_execution_sequencer_retry_prompt(
            sequencing_input=sequencing_input,
            validation_error=str(exc),
        )

        raw_retry = provider.generate_structured(
            system_prompt=EXECUTION_SEQUENCER_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="execution_plan",
            json_schema=ExecutionPlan.model_json_schema(),
        )
        raw_retry = _ensure_final_checkpoint_stage_closure(raw_retry)

        result = ExecutionPlan.model_validate(raw_retry)

    _append_sequencer_trace(
        project_id=project_id,
        call_type=call_type,
        sequencing_input=sequencing_input,
        result=result,
    )
    return result
