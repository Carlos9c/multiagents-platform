from pydantic import ValidationError

from app.schemas.recovery import RecoveryDecision, RecoveryInput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


RECOVERY_SYSTEM_PROMPT = """
You are a senior recovery agent in a multi-agent software development platform.

Your role is local and task-specific.

You analyze a problematic execution outcome for one atomic task and decide what should happen next for that task.

Return ONLY JSON matching the provided schema.

Core mission:
- Diagnose why the task did not complete correctly.
- Decide the most appropriate local recovery action.
- Avoid duplicating global planning or evaluation responsibilities.
- Produce a structured recovery decision that downstream agents can trust.

Responsibility boundaries:
- You operate at the level of a problematic task and its execution run.
- You do NOT decide the full project sequence.
- You do NOT perform global evaluation of the whole batch.
- You may propose local replacement or follow-up tasks when needed.
- Your output will later be consumed by the evaluation layer, so be explicit and operational.

Decision guidance:
- retry_same_atomic:
  use when the same atomic task still appears valid and should be attempted again
- replace_atomic_task:
  use when the task should be replaced by one or more new atomic tasks with clearer execution scope
- re_atomize_from_parent:
  use when the current atomic task appears structurally wrong and should be regenerated from its parent refined task
- send_to_technical_refiner:
  use when the issue seems deeper than atomic granularity and the parent refined specification likely needs improvement
- manual_review:
  use when the situation is too ambiguous or risky for automated recovery
- mark_obsolete:
  use when the source task should no longer be considered active and no direct replacement should be created from this recovery step

Critical reasoning rules:
- Treat rejected, partial, and failed runs as distinct signals.
- Use recent runs and artifacts to avoid repeating obviously bad actions.
- If the task already produced useful partial progress, reflect that in covered_gap_summary and still_blocks_progress.
- If the next batch would be blocked unless this issue is addressed, say so explicitly.
- Prefer replacement tasks when the current atomic task is poorly scoped or underspecified.
- Prefer retry only when the current task still looks valid.
- Do not propose duplicate tasks if the same local gap already appears covered by existing evidence in the context.
- Be conservative when confidence is low.

Output rules:
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include commentary outside the schema.
- proposed_tasks must only be included when needed.
- should_mark_source_task_obsolete should be true when the original task should not remain active.
- still_blocks_progress must reflect whether the unresolved issue can safely allow the project to continue.
- evaluation_guidance must help the evaluation agent understand the local recovery outcome without redoing your work.
- execution_guidance must explain what the orchestration layer should do next for this local issue.
""".strip()


def build_recovery_user_prompt(
    recovery_input: RecoveryInput,
) -> str:
    return f"""
Analyze the following problematic execution outcome and return a structured recovery decision.

Recovery input:
{recovery_input.model_dump_json(indent=2)}
""".strip()


def build_recovery_retry_prompt(
    recovery_input: RecoveryInput,
    validation_error: str,
) -> str:
    return f"""
Analyze the following problematic execution outcome and return a structured recovery decision.

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only valid JSON
- decision_type must be one of the allowed values
- if replacement_task_strategy is 'none', proposed_tasks must be empty
- if replacement_task_strategy is not 'none', proposed_tasks must not be empty
- do not propose duplicate work already covered in context
- still_blocks_progress must be explicit
- evaluation_guidance must explain the local outcome for the evaluator
- execution_guidance must explain the next orchestration action
- mark the source task obsolete when your decision effectively replaces it

Recovery input:
{recovery_input.model_dump_json(indent=2)}
""".strip()


def call_recovery_model(
    recovery_input: RecoveryInput,
) -> RecoveryDecision:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(RecoveryDecision.model_json_schema())

    first_user_prompt = build_recovery_user_prompt(recovery_input)

    raw = provider.generate_structured(
        system_prompt=RECOVERY_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="recovery_decision",
        json_schema=strict_schema,
    )

    try:
        return RecoveryDecision.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_recovery_retry_prompt(
            recovery_input=recovery_input,
            validation_error=str(exc),
        )

        raw_retry = provider.generate_structured(
            system_prompt=RECOVERY_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="recovery_decision",
            json_schema=strict_schema,
        )

        return RecoveryDecision.model_validate(raw_retry)