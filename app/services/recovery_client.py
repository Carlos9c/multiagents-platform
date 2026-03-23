from pydantic import ValidationError

from app.schemas.recovery import RecoveryDecision
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


RECOVERY_SYSTEM_PROMPT = """
You are a senior recovery decision agent.

Your job is to decide the narrowest reliable recovery action after a task failed, was rejected,
or was validated as partial/failed.
Return ONLY JSON matching the provided schema.

Current workflow reality:
- The platform plans at high_level and decomposes directly into atomic tasks by default.
- Technical refinement is not part of the active recovery workflow.
- Do NOT suggest refined-level recovery.
- Do NOT suggest legacy actions such as:
  - send_to_technical_refiner
  - replace_atomic_task
  - re_atomize_from_parent
  - mark_obsolete
- The only valid recovery actions are:
  - retry
  - reatomize
  - insert_followup
  - manual_review

Action semantics:
- retry
  - use only when the same task is still valid as-is
  - use only when the failure appears transient, local, or execution-related
  - do not create new tasks
- reatomize
  - use when the current task was badly scoped, not executable as one unit, too broad,
    mixed incompatible work, or should be split into better atomic tasks
  - this action must create replacement atomic tasks
- insert_followup
  - use when the original task produced useful progress, but extra atomic work is needed
    to close a remaining gap
  - this action must create one or more follow-up atomic tasks
  - do not use this as a vague catch-all when the original task should really be reatomized
- manual_review
  - use when automated recovery is not trustworthy enough
  - do not create new tasks

Decision rules:
- Prefer the narrowest sufficient action.
- Prefer retry over reatomize only when the task itself is still structurally sound.
- Prefer reatomize over insert_followup when the original task definition is the real problem.
- Prefer insert_followup only when the task was mostly valid and the remaining work is additive.
- Use manual_review for ambiguity, conflict, unsafe state, or lack of reliable automated next steps.

Executor compatibility rules:
- Any created task must be executable by the current system.
- Assume the active executor is code_executor unless the context explicitly proves otherwise.
- Created tasks must be concrete atomic tasks with a repository/file-oriented outcome.
- Do not create tasks centered on manual investigation, external research, or human-only validation.

Created task quality rules:
- title must be concrete and actionable
- description must clearly define the deliverable
- objective should state the intended outcome
- implementation_notes should explain the practical approach
- acceptance_criteria should describe what must be true for the task to be done
- technical_constraints should include meaningful limits when relevant
- out_of_scope should explicitly exclude nearby but different work
- do not create vague tasks
- do not create pseudo-epics
- do not create non-executable tasks

Reasoning rules:
- reason must explain why the selected action is the best recovery mechanism
- covered_gap_summary must explain the specific gap being addressed
- still_blocks_progress should be true when downstream progress remains blocked until this recovery is applied
- evaluation_guidance may explain how the evaluator should interpret this recovery choice
- execution_guidance may explain constraints or expectations for the executor

Self-check before finalizing:
- Is this action one of the four valid actions?
- Is it the narrowest reliable action?
- If action=retry, are created_tasks empty and retry_same_task=true?
- If action=reatomize or insert_followup, are created_tasks present and clearly atomic?
- If action=manual_review, are created_tasks empty and requires_manual_review=true?
- Are all created tasks compatible with code_executor and repository-based validation?
""".strip()


def build_recovery_user_prompt(
    *,
    execution_context_summary: str,
    validation_context_summary: str,
    next_batch_summary: str | None,
    remaining_plan_summary: str | None,
) -> str:
    return f"""
Execution context summary:
{execution_context_summary}

Validation context summary:
{validation_context_summary}

Next batch summary:
{next_batch_summary or "None"}

Remaining plan summary:
{remaining_plan_summary or "None"}

Instructions:
- Choose the narrowest reliable recovery action.
- Use retry only if the same task should be attempted again without changing its structure.
- Use reatomize if the task itself is structurally wrong as one atomic unit.
- Use insert_followup only if the original task was still valid but additional atomic work is needed.
- Use manual_review if automated recovery is not trustworthy enough.

Important:
- Do not use refined-level recovery.
- Do not propose legacy recovery actions.
- Any created tasks must be atomic, executor-compatible, and repository/file-oriented.
- Avoid vague or human-only tasks.
- Be operational, strict, and concrete.
""".strip()


def build_recovery_retry_prompt(
    *,
    validation_error: str,
    execution_context_summary: str,
) -> str:
    return f"""
Your previous recovery output was invalid.

Validation error:
{validation_error}

Execution context summary:
{execution_context_summary}

You must correct the output and return valid JSON matching the schema.

Critical corrections:
- valid actions are only: retry, reatomize, insert_followup, manual_review
- do not use refined-level or legacy recovery actions
- if action=retry:
  - retry_same_task must be true
  - created_tasks must be empty
- if action=reatomize or action=insert_followup:
  - created_tasks must not be empty
  - created tasks must be concrete atomic tasks compatible with code_executor
- if action=manual_review:
  - requires_manual_review must be true
  - created_tasks must be empty
- keep the action narrow, concrete, and operationally valid
""".strip()


def call_recovery_model(
    *,
    execution_context_summary: str,
    validation_context_summary: str,
    next_batch_summary: str | None = None,
    remaining_plan_summary: str | None = None,
) -> RecoveryDecision:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        RecoveryDecision.model_json_schema()
    )

    first_user_prompt = build_recovery_user_prompt(
        execution_context_summary=execution_context_summary,
        validation_context_summary=validation_context_summary,
        next_batch_summary=next_batch_summary,
        remaining_plan_summary=remaining_plan_summary,
    )

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
            validation_error=str(exc),
            execution_context_summary=execution_context_summary,
        )

        raw_retry = provider.generate_structured(
            system_prompt=RECOVERY_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="recovery_decision",
            json_schema=strict_schema,
        )

        return RecoveryDecision.model_validate(raw_retry)