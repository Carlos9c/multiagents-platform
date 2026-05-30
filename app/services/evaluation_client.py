from pydantic import ValidationError

from app.schemas.evaluation import StageEvaluationOutput
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

STAGE_EVALUATION_SYSTEM_PROMPT = prompt_loader.get("stage_evaluator")


def build_stage_evaluation_user_prompt(
    *,
    project_name: str,
    project_description: str,
    stage_goal: str,
    stage_scope_summary: str,
    processed_batch_summary: str,
    task_state_summary: str,
    recovery_context_summary: str,
    recovery_tasks_created_summary: str,
    remaining_batches_summary: str,
    pending_task_summary: str,
    checkpoint_artifact_window_summary: str,
    additional_context: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "stage_evaluator",
        "main",
        {
            "project_name": project_name,
            "project_description": project_description,
            "stage_goal": stage_goal,
            "stage_scope_summary": stage_scope_summary,
            "processed_batch_summary": processed_batch_summary,
            "task_state_summary": task_state_summary,
            "recovery_context_summary": recovery_context_summary,
            "recovery_tasks_created_summary": recovery_tasks_created_summary,
            "remaining_batches_summary": remaining_batches_summary,
            "pending_task_summary": pending_task_summary,
            "checkpoint_artifact_window_summary": checkpoint_artifact_window_summary,
            "additional_context": additional_context,
        },
    )
    return f"""
Project name: {project_name}
Project description: {project_description}

Stage goal:
{stage_goal}

Stage scope summary:
{stage_scope_summary}

Processed batch summary:
{processed_batch_summary}

Task state summary:
{task_state_summary}

Recovery context summary:
{recovery_context_summary}

Recovery tasks created summary:
{recovery_tasks_created_summary}

Remaining batches summary:
{remaining_batches_summary}

Pending task summary:
{pending_task_summary}

Checkpoint artifact window summary:
{checkpoint_artifact_window_summary}

Additional context:
{additional_context}

Operational instructions:
- evaluate the CURRENT STAGE, not only the last task or last batch
- determine whether the stage can be closed now
- if the stage is incomplete, choose the narrowest reliable next recovery mechanism
- explicitly choose the next operational action using recommended_next_action
- fill the structured reasoning fields:
  - decision_signals
  - plan_change_scope
  - remaining_plan_still_valid
  - new_recovery_tasks_blocking
  - single_task_tail_risk
- prefer atomic-level correction when the issue is local
- escalate to high-level replanning only when the current high-level plan is no longer adequate
- do not use refined as a planning or replanning level
- do not assume technical refinement exists in the active workflow
- do not request manual review unless automated recovery is genuinely unreliable
- do not use retry_batch

Decision reminders:
- stage_completed requires project_stage_closed=true
- stage_completed must not require manual review
- if recovery_strategy is replan_from_high_level, then replan.required must be true and replan.level must be high_level
- if recovery_strategy is insert_followup_atomic_tasks, then followup_atomic_tasks_required must be true
- if recovery_strategy is manual_review, then manual_review_required must be true
- if recovery_strategy is reatomize_failed_tasks, keep replanning at the atomic layer rather than high_level
- a local context-selection failure should not escalate to manual review or high-level replanning unless repeated evidence clearly justifies it

Next action reminders:
- use close_stage only if the stage is truly complete
- use continue_current_plan when the current remaining plan already represents the correct next work
- use resequence_remaining_batches when the remaining work is still basically right but should be regrouped or reprioritized
- use replan_remaining_work only when the remaining plan no longer represents the stage adequately
- use manual_review only when automation is not trustworthy enough
- a newly created non-critical follow-up task may justify resequence_remaining_batches instead of continue_current_plan if regrouping avoids an awkward one-task validation loop
- a newly created follow-up task does NOT automatically justify high-level replanning

What to optimize for:
- operational correctness
- minimal sufficient correction
- stage-level truthfulness
- internally consistent output
""".strip()


def build_stage_evaluation_retry_prompt(
    *,
    original_user_prompt: str,
    validation_error: str,
) -> str:
    prompt_loader.validate_builder_inputs(
        "stage_evaluator",
        "retry",
        {
            "original_user_prompt": original_user_prompt,
            "validation_error": validation_error,
        },
    )
    return f"""
Your previous output was invalid. Correct it and return valid JSON matching the schema.

Validation error:
{validation_error}

Critical corrections:
- do not use refined as a replan level
- only valid replan levels are atomic and high_level
- do not use retry_batch
- keep decision, project_stage_closed, manual_review_required, recovery_strategy, and replan fully consistent
- do not set stage_completed unless the stage is truly closed
- do not request replan_from_high_level unless replan.required=true and replan.level=high_level
- do not request insert_followup_atomic_tasks unless followup_atomic_tasks_required=true
- do not request manual_review unless manual_review_required=true
- prefer the narrowest sufficient recovery action
- do not escalate a local recoverable context-selection failure to manual review unless the evidence clearly requires it
- return only JSON matching the schema

Original evaluation context:
{original_user_prompt}
""".strip()


def call_stage_evaluation_model(
    *,
    project_name: str,
    project_description: str,
    stage_goal: str,
    stage_scope_summary: str,
    processed_batch_summary: str,
    task_state_summary: str,
    recovery_context_summary: str,
    recovery_tasks_created_summary: str,
    remaining_batches_summary: str,
    pending_task_summary: str,
    checkpoint_artifact_window_summary: str,
    additional_context: str = "",
) -> StageEvaluationOutput:
    provider = get_llm_provider()
    first_user_prompt = build_stage_evaluation_user_prompt(
        project_name=project_name,
        project_description=project_description,
        stage_goal=stage_goal,
        stage_scope_summary=stage_scope_summary,
        processed_batch_summary=processed_batch_summary,
        task_state_summary=task_state_summary,
        recovery_context_summary=recovery_context_summary,
        recovery_tasks_created_summary=recovery_tasks_created_summary,
        remaining_batches_summary=remaining_batches_summary,
        pending_task_summary=pending_task_summary,
        checkpoint_artifact_window_summary=checkpoint_artifact_window_summary,
        additional_context=additional_context,
    )

    raw = provider.generate_structured(
        system_prompt=STAGE_EVALUATION_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="stage_evaluation_output",
        json_schema=StageEvaluationOutput.model_json_schema(),
    )

    try:
        return StageEvaluationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_stage_evaluation_retry_prompt(
            original_user_prompt=first_user_prompt,
            validation_error=str(exc),
        )

        raw_retry = provider.generate_structured(
            system_prompt=STAGE_EVALUATION_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="stage_evaluation_output",
            json_schema=StageEvaluationOutput.model_json_schema(),
        )

        return StageEvaluationOutput.model_validate(raw_retry)
