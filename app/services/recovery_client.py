from pydantic import ValidationError

from app.execution_engine.capabilities import render_executor_capabilities_for_prompt
from app.models.task import EXECUTION_ENGINE
from app.schemas.recovery import RecoveryDecision
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

RECOVERY_SYSTEM_PROMPT = prompt_loader.get("recovery_planner")


def build_recovery_user_prompt(
    *,
    source_task_summary: str,
    execution_trajectory_summary: str,
    execution_context_summary: str,
    validation_context_summary: str,
    next_batch_summary: str | None,
    remaining_plan_summary: str | None,
    relevant_file_contents: str | None = None,
) -> str:
    capability_text = render_executor_capabilities_for_prompt(EXECUTION_ENGINE)

    prompt_loader.validate_builder_inputs(
        "recovery_planner",
        "main",
        {
            "source_task_summary": source_task_summary,
            "execution_trajectory_summary": execution_trajectory_summary,
            "execution_context_summary": execution_context_summary,
            "validation_context_summary": validation_context_summary,
            "next_batch_summary": next_batch_summary,
            "remaining_plan_summary": remaining_plan_summary,
            "relevant_file_contents": relevant_file_contents,
            "execution_engine_capabilities": capability_text,
        },
    )

    relevant_files_section = ""
    if relevant_file_contents:
        relevant_files_section = f"""

Relevant source files (read during the failed execution — use these to write accurate API references):
{relevant_file_contents}
"""

    return f"""
Source task summary:
{source_task_summary}

Execution trajectory summary:
{execution_trajectory_summary}

Execution context summary:
{execution_context_summary}

Validation context summary:
{validation_context_summary}

Next batch summary:
{next_batch_summary or "None"}

Remaining plan summary:
{remaining_plan_summary or "None"}
{relevant_files_section}
Execution engine capability catalog:
{capability_text}

Instructions:
- Choose the narrowest reliable recovery action.
- Preserve the original task intent unless there is strong evidence that the task itself is structurally wrong.
- Recovery only applies to problematic outcomes, not fully completed tasks.
- Use last_execution_agent_sequence as a primary clue for what actually happened during execution.
- Treat validation as structured operational evidence, not as something to re-evaluate from scratch.
- In recovery, assume validation will normally indicate partial, failed, or manual_review states.
- Use validation.decision and validation.summary to understand the recovery posture quickly.
- Use validation.validated_scope to preserve useful partial progress already achieved.
- Use validation.missing_scope as the primary signal for narrow follow-up work when the original task remains valid.
- Use validation.blockers to distinguish operational impediments from structural task-definition problems.
- If validation.manual_review_required is true, be conservative and prefer manual_review unless there is strong evidence for a safe automated action.
- If validation.followup_validation_required is true, do not assume the task should be reatomized by default.
- Use reatomize if the task itself is structurally wrong as one atomic unit, but keep the same workstream intent.
- Use insert_followup only if the original task was still valid but additional atomic work is needed.
- Use manual_review if automated recovery is not trustworthy enough.

Important:
- Valid actions are only: reatomize, insert_followup, manual_review.
- Do not use retry.
- Do not use refined-level recovery.
- Do not propose legacy recovery actions.
- Any created tasks must be atomic, execution-engine-compatible, and repository/file-oriented.
- Use the listed execution-engine subagents and tools as the source of truth for what is realistically doable.
- Avoid vague or human-only tasks.
- Do not change a documentation/scope/requirements task into implementation/bootstrap work unless the evidence clearly requires that.
- If the failure is mainly about context selection, treat it first as a context-resolution problem rather than an intent-change problem.
- Do not assume that a multi-agent execution route automatically means the task should be reatomized.
- Be operational, strict, conservative, and concrete.
""".strip()


def build_recovery_retry_prompt(
    *,
    validation_error: str,
    source_task_summary: str,
    execution_trajectory_summary: str,
    execution_context_summary: str,
    validation_context_summary: str,
) -> str:
    capability_text = render_executor_capabilities_for_prompt(EXECUTION_ENGINE)
    prompt_loader.validate_builder_inputs(
        "recovery_planner",
        "retry",
        {
            "source_task_summary": source_task_summary,
            "execution_trajectory_summary": execution_trajectory_summary,
            "execution_context_summary": execution_context_summary,
            "validation_context_summary": validation_context_summary,
            "validation_error": validation_error,
            "executor_capabilities": capability_text,
        },
    )
    return f"""
Your previous recovery output was invalid.

Validation error:
{validation_error}

Source task summary:
{source_task_summary}

Execution trajectory summary:
{execution_trajectory_summary}

Execution context summary:
{execution_context_summary}

Validation context summary:
{validation_context_summary}

Execution engine capability catalog:
{capability_text}

You must correct the output and return valid JSON matching the schema.

Critical corrections:
- valid actions are only: reatomize, insert_followup, manual_review
- retry is not allowed in the current workflow
- do not use refined-level or legacy recovery actions
- preserve the original task intent and workstream
- recovery applies only to problematic outcomes, not fully completed tasks
- use last_execution_agent_sequence as contextual evidence, not as an automatic trigger
- use validation as structured operational evidence, not as a prompt to re-validate the task
- if validation.validated_scope shows useful partial progress, preserve it
- if validation.missing_scope describes a narrow remaining gap, prefer insert_followup over reatomize unless the source task is structurally wrong
- if validation.manual_review_required is true, prefer manual_review unless there is strong evidence for safe automated recovery
- if validation.followup_validation_required is true, do not escalate to reatomize by default
- do not silently change documentation/scope work into implementation/bootstrap work
- if the failure is mainly about context selection, prefer conservative reatomize or manual_review over domain-changing recovery
- if action=reatomize or action=insert_followup:
  - created_tasks must not be empty
  - created tasks must be concrete atomic tasks compatible with the execution engine
  - created tasks must remain faithful to the source task intent
- if action=manual_review:
  - requires_manual_review must be true
  - created_tasks must be empty
- use the listed execution-engine subagents and tools as the source of truth for what is realistically doable
- keep the action narrow, concrete, and operationally valid
""".strip()


def call_recovery_model(
    *,
    source_task_summary: str,
    execution_trajectory_summary: str,
    execution_context_summary: str,
    validation_context_summary: str,
    next_batch_summary: str | None = None,
    remaining_plan_summary: str | None = None,
    relevant_file_contents: str | None = None,
) -> RecoveryDecision:
    provider = get_llm_provider()
    first_user_prompt = build_recovery_user_prompt(
        source_task_summary=source_task_summary,
        execution_trajectory_summary=execution_trajectory_summary,
        execution_context_summary=execution_context_summary,
        validation_context_summary=validation_context_summary,
        next_batch_summary=next_batch_summary,
        remaining_plan_summary=remaining_plan_summary,
        relevant_file_contents=relevant_file_contents,
    )

    raw = provider.generate_structured(
        system_prompt=RECOVERY_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="recovery_decision",
        json_schema=RecoveryDecision.model_json_schema(),
    )

    try:
        return RecoveryDecision.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_recovery_retry_prompt(
            validation_error=str(exc),
            source_task_summary=source_task_summary,
            execution_trajectory_summary=execution_trajectory_summary,
            execution_context_summary=execution_context_summary,
            validation_context_summary=validation_context_summary,
        )

        raw_retry = provider.generate_structured(
            system_prompt=RECOVERY_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="recovery_decision",
            json_schema=RecoveryDecision.model_json_schema(),
        )

        return RecoveryDecision.model_validate(raw_retry)
