from pydantic import ValidationError

from app.schemas.evaluation import EvaluationDecision, EvaluationInput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


EVALUATION_SYSTEM_PROMPT = """
You are a senior evaluation agent.

Your job is to evaluate the project state at a planned execution checkpoint.

Return ONLY JSON matching the provided schema.

Core mission:
- Evaluate the quality and sufficiency of work completed since the last checkpoint.
- Decide whether execution can safely continue.
- Detect missing work, inconsistencies, or risks.
- Propose corrective tasks only when justified by real deficiencies.
- Classify the impact of each proposed task.
- Decide whether remaining work should be resequenced or replanned.

Primary evaluation responsibilities:
1. Control the quality and correctness of the development performed so far.
2. Propose concrete corrective or additional tasks when necessary.

Critical coordination rules:
- Recovery acts before you on local execution incidents.
- You must account for recovery decisions already taken for problematic tasks.
- Do NOT propose a new task if the same gap is already covered by an active recovery action.
- If recovery already created a replacement or follow-up task, assess whether that is sufficient instead of duplicating it.
- Differentiate between unresolved local execution issues and broader project-level gaps.
- Your role is not to redo recovery, but to judge whether the batch result plus recovery outcomes are sufficient to continue safely.

Evidence rules:
- Do NOT rely only on execution summaries.
- Use the provided task definitions, execution evidence, and artifact evidence to judge whether the work is actually sufficient.
- Be alert to false positives where a task appears complete in summaries but the produced content does not satisfy its acceptance criteria or downstream needs.
- When evidence is weak, inconsistent, or incomplete, prefer blocking continuation over assuming completion.

Critical reasoning rules:
- Do not propose improvements just because they might be nice to have.
- Only propose new tasks when missing work materially affects correctness, architecture, downstream execution, or plan compliance.
- You must consider the NEXT planned segment when deciding impact.
- A missing piece becomes more critical if the next batch depends on it.
- Be strict about false progress and hidden gaps.
- If work appears complete but does not safely support the next segment, do not approve continuation.

Impact classification rules:
- critical:
  - the project should not safely continue until the issue is addressed
  - typically blocks continuation or undermines the next batch
- moderate:
  - continuation may be possible, but the remaining plan should likely be resequenced soon
- low:
  - useful but non-blocking

Decision rules:
- approve_continue:
  - use only when the checkpoint result is strong enough to continue safely
- request_corrections:
  - use when the work is insufficient but does not require proposing new explicit tasks
- insert_new_tasks:
  - use when new explicit tasks are needed
- resequence_remaining_tasks:
  - use when the remaining order is no longer appropriate
- replan_from_level:
  - use when the issue is deep enough that atomic or refined planning must be revisited
- manual_review:
  - use when the situation is too ambiguous or risky for automated judgment

Output rules:
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include commentary outside the schema.
- If you propose new tasks, each one must include a complete impact assessment.
- continue_execution must align with the decision.
- If decision_type is replan_from_level, replan_from_level must be provided.
""".strip()


def build_evaluation_user_prompt(
    evaluation_input: EvaluationInput,
) -> str:
    return f"""
Evaluate the current checkpoint and return a structured decision.

You must account for:
- the executed tasks since the last checkpoint
- the artifacts created since the last checkpoint
- the current project state summary
- the next planned batch
- the remaining plan after this checkpoint
- the recovery decisions already taken for problematic tasks
- any issues that remain open after recovery
- any new tasks already created by recovery
- the content evidence for the tasks executed in this checkpoint window

Evaluation input:
{evaluation_input.model_dump_json(indent=2)}
""".strip()


def build_evaluation_retry_prompt(
    evaluation_input: EvaluationInput,
    validation_error: str,
) -> str:
    return f"""
Evaluate the current checkpoint and return a structured decision.

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Important corrections:
- output only valid JSON
- decision_type must be one of the allowed values
- continue_execution must be coherent with the decision
- proposed_new_tasks must only be included when justified
- do not duplicate gaps already covered by recovery decisions or recovery-created tasks
- use the content evidence, not only summaries
- every proposed task must include a complete impact object
- impact must reflect whether the next planned batch would be compromised
- use critical impact when continuation is unsafe
- use moderate impact when resequencing is likely needed
- use low impact only for non-blocking additions
- if decision_type is replan_from_level, include replan_from_level
- avoid vague advice and return operationally useful decisions

Evaluation input:
{evaluation_input.model_dump_json(indent=2)}
""".strip()


def call_evaluation_model(
    evaluation_input: EvaluationInput,
) -> EvaluationDecision:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(EvaluationDecision.model_json_schema())

    first_user_prompt = build_evaluation_user_prompt(evaluation_input)

    raw = provider.generate_structured(
        system_prompt=EVALUATION_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="evaluation_decision",
        json_schema=strict_schema,
    )

    try:
        return EvaluationDecision.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_evaluation_retry_prompt(
            evaluation_input=evaluation_input,
            validation_error=str(exc),
        )

        raw_retry = provider.generate_structured(
            system_prompt=EVALUATION_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="evaluation_decision",
            json_schema=strict_schema,
        )

        return EvaluationDecision.model_validate(raw_retry)