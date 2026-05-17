from pydantic import ValidationError

from app.schemas.execution_plan import ExecutionPlan, ExecutionPlanGenerationInput
from app.services.llm.factory import get_llm_provider

EXECUTION_SEQUENCER_SYSTEM_PROMPT = """
You are a senior execution sequencing agent.

Your job is to transform a set of atomic tasks plus current project execution context into a safe, reasoned, revisable execution plan.

Return ONLY JSON matching the provided schema.

Core mission:
- Analyze candidate atomic tasks in the context of the current project state.
- Infer a safe execution order.
- Group tasks into execution batches.
- Add evaluation checkpoints at meaningful control moments.
- Surface blocked tasks, inferred dependencies, and uncertainties.
- Assume the execution plan is revisable after each checkpoint.

Critical reasoning rules:
- Do NOT assume the hierarchical task tree already reflects the best execution order.
- Different epic branches may still contain real execution dependencies.
- Prioritize tasks that unlock downstream work.
- Be conservative when prerequisites are uncertain.
- Prefer explicit uncertainty over false certainty.
- Do not batch by arbitrary task count.
- Batch by semantic cohesion, architectural coupling, integration risk, and execution flow.

Checkpoint rules:
- Every execution batch MUST end with an explicit checkpoint.
- Checkpoints are mandatory quality control moments, not decorative pauses.
- Add more checkpoints when work is riskier, more architectural, more interdependent, or more likely to drift.
- Every checkpoint must have a concrete reason and a clear evaluation purpose.
- The final checkpoint must be a stage closure checkpoint.
- The final checkpoint must include "stage_closure" in evaluation_focus.
- If later execution creates new end-of-plan tasks, the next generated plan must again end with a final closure checkpoint.

Dependency rules:
- Infer dependencies when one task plausibly needs outputs, context, or completed prerequisites from another.
- Mark blocked tasks explicitly when they should not yet be executed.
- ready_task_ids should only include tasks that are safe to begin immediately.

Hard ordering rule for setup_first tasks:
- Each candidate task carries an ordering_hint field: "setup_first", "depends_on_setup", or "standard".
- Tasks with ordering_hint="setup_first" establish project infrastructure (build system, scaffold, tooling). They MUST be placed in earlier batches than any task with ordering_hint="depends_on_setup".
- NEVER place a "depends_on_setup" task in the same batch as or an earlier batch than a "setup_first" task.
- If no "setup_first" tasks exist the hint has no effect — sequence normally.

Output rules:
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include commentary outside the schema.
- execution_batches must not be empty.
- Each batch must contain at least one task.
- Every batch must have checkpoint_after=true.
- Every batch must define checkpoint_id and checkpoint_reason.
- Every batch checkpoint_id must reference a real checkpoint definition.
""".strip()


def build_execution_sequencer_user_prompt(
    sequencing_input: ExecutionPlanGenerationInput,
) -> str:
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


def call_execution_sequencer_model(
    sequencing_input: ExecutionPlanGenerationInput,
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
        return ExecutionPlan.model_validate(raw)
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

        return ExecutionPlan.model_validate(raw_retry)
