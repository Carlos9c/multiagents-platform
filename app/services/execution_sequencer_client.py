from pydantic import ValidationError

from app.schemas.execution_plan import ExecutionPlan, ExecutionPlanGenerationInput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


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

Execution sequencing input:
{sequencing_input.model_dump_json(indent=2)}
""".strip()


def call_execution_sequencer_model(
    sequencing_input: ExecutionPlanGenerationInput,
) -> ExecutionPlan:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(ExecutionPlan.model_json_schema())

    first_user_prompt = build_execution_sequencer_user_prompt(sequencing_input)

    raw = provider.generate_structured(
        system_prompt=EXECUTION_SEQUENCER_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="execution_plan",
        json_schema=strict_schema,
    )

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
            json_schema=strict_schema,
        )

        return ExecutionPlan.model_validate(raw_retry)
