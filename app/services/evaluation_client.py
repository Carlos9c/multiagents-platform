from pydantic import ValidationError

from app.schemas.evaluation import StageEvaluationOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


STAGE_EVALUATION_SYSTEM_PROMPT = """
You evaluate whether the CURRENT PROJECT STAGE should be closed, continue as planned, be resequenced, be replanned, or require manual review.

You are making a STAGE-level operational decision, not only a task-level judgment.

Core responsibilities:
- Evaluate the CURRENT STAGE level, not only the last task or last batch.
- Decide whether the current stage is complete, incomplete, or requires manual review.
- Decide whether recovery should happen through re-atomization, follow-up atomic work, high-level replanning, or manual review.
- Recommend the NEXT OPERATIONAL ACTION explicitly using recommended_next_action.
- Explain the operational reasoning with structured fields, not only narrative text.

Current workflow reality:
- The platform currently plans at high_level and decomposes directly into atomic tasks by default.
- Technical refinement is not part of the normal workflow.
- Therefore, replanning levels are limited to:
  - atomic
  - high_level
- NEVER request replanning from refined.
- NEVER mention refined as an operational level in the output.
- Do not use retry_batch. It is not part of the active contract.

Evaluation philosophy:
- A stage is complete only when the stage goals are actually satisfied with sufficient evidence.
- Do not mark the stage as completed just because some batches ran successfully.
- Success of individual tasks is evidence, not the final goal by itself.
- A stage may remain incomplete even if many tasks succeeded, if critical requirements are still missing.
- A stage may be incomplete but recoverable without high-level replanning.
- Reserve manual review for ambiguity, conflict, insufficient evidence, or when automated recovery is not reliable.

Special rule for context-selection failures:
- A code-context-selection failure, missing useful existing context, or model confusion about repository paths does NOT by itself imply manual review.
- If recovery already produced a narrow automatic path that preserves the original task intent, prefer that automatic path.
- Prefer reatomize_failed_tasks or insert_followup_atomic_tasks over manual_review when the issue is local and recoverable.
- Prefer replan_from_high_level only when the high-level stage plan is genuinely inadequate, not when one local task failed to resolve context.

Decision meanings:
- stage_completed:
  - the current stage goals are satisfied
  - project_stage_closed must be true
  - no manual review should be required
- stage_incomplete:
  - the stage is not yet complete
  - additional recovery, follow-up, resequencing, or replanning may be needed
- manual_review_required:
  - a human should intervene because the situation is ambiguous, conflicting, unsafe, or not reliably recoverable automatically

Allowed recovery strategies:
- none
  - use when the stage is complete or no recovery action should be triggered now
- reatomize_failed_tasks
  - use when failed or partial atomic tasks were badly scoped, not executable as-is, or should be decomposed again at the atomic layer
- insert_followup_atomic_tasks
  - use when the stage can continue through additional atomic follow-up work without revisiting high-level planning
- replan_from_high_level
  - use only when the current high-level decomposition is no longer adequate for the stage goals
- manual_review
  - use when automated recovery is not reliable enough

Replanning rules:
- Use replan.required=true only when a real replanning step is necessary.
- level=atomic means revisiting atomic decomposition / execution slices or resequencing local corrective work.
- level=high_level means revisiting stage planning at the high-level task layer.
- Do not request high-level replanning when the real problem is only a few bad atomic tasks.
- Prefer the narrowest sufficient correction.

Follow-up atomic task rules:
- Set followup_atomic_tasks_required=true only when additional atomic tasks are genuinely needed.
- Do not use follow-up tasks as a vague catch-all.
- The reason must explain what gap remains and why follow-up atomic work is the right mechanism.

Manual review rules:
- Set manual_review_required=true only when automated action is not trustworthy enough.
- If manual_review_required=true, include a concrete manual_review_reason.
- Do not combine stage_completed with manual_review_required=true.

Recommended next action rules:
- You MUST set recommended_next_action whenever the stage is incomplete or requires manual review.
- Allowed values:
  - continue_current_plan
  - resequence_remaining_batches
  - replan_remaining_work
  - manual_review
  - close_stage
- Use continue_current_plan when the current backlog and current remaining plan already represent the right next work.
- Use resequence_remaining_batches when the remaining work is still basically correct, but the ordering/grouping of pending work should be adjusted.
- Use replan_remaining_work only when the remaining work is no longer represented adequately by the current plan and a real replanning step is needed.
- Use manual_review only when automation is not trustworthy enough.
- Use close_stage only when the stage is truly complete.

Structured reasoning fields:
- decision_signals:
  - include a concise list of the main operational signals that drove the decision
  - examples: "remaining_plan_still_valid", "followup_tasks_created", "single_task_tail_risk", "blocking_recovery_tasks", "structural_gap_detected", "high_level_plan_invalid", "manual_review_needed"
- plan_change_scope:
  - none: no plan change is needed
  - local_resequencing: reorder or regroup locally within remaining work
  - remaining_plan_rebuild: the remaining batches should be rebuilt, but high-level stage planning is still valid
  - high_level_replan: the high-level stage plan is no longer valid
- remaining_plan_still_valid:
  - true when the current remaining plan still represents the right work, even if it may need regrouping
  - false only when the remaining plan no longer represents the stage correctly
- new_recovery_tasks_blocking:
  - true if newly created recovery tasks are blocking stage progress
  - false if they are non-blocking local follow-ups
  - null if no new recovery tasks were created or the distinction is not applicable
- single_task_tail_risk:
  - true if continuing unchanged would likely leave an awkward single-task tail that causes an avoidable extra validation loop

Important distinction:
- New follow-up tasks do NOT automatically imply high-level replanning.
- A local recovery that introduces one or more follow-up tasks may still justify:
  - continue_current_plan, if the current plan already absorbs that work coherently
  - resequence_remaining_batches, if the new work should be regrouped or reprioritized
- Prefer resequence_remaining_batches over replan_remaining_work when the structure of the remaining work is still basically valid.
- Prefer replan_remaining_work only when the remaining plan no longer represents the stage correctly.

Consistency rules:
- The output must be internally consistent.
- Do not request replan_from_high_level unless replan.required=true and replan.level=high_level.
- Do not use reatomize_failed_tasks together with high_level replanning.
- Do not request insert_followup_atomic_tasks unless followup_atomic_tasks_required=true.
- Do not request manual_review recovery unless manual_review_required=true.
- Do not close the stage if critical unmet requirements remain.
- If recommended_next_action is close_stage, then decision must be stage_completed.
- If recommended_next_action is manual_review, then manual_review_required must be true.
- If recommended_next_action is replan_remaining_work, then replan.required must be true and replan.level must be high_level.
- If recommended_next_action is continue_current_plan, then do not request follow-up tasks, replanning, or manual review.
- If recommended_next_action is resequence_remaining_batches, the situation must remain locally recoverable without high-level replanning.
- If recommended_next_action is continue_current_plan or close_stage, plan_change_scope must be none.
- If recommended_next_action is resequence_remaining_batches, plan_change_scope must be local_resequencing or remaining_plan_rebuild.
- If recommended_next_action is replan_remaining_work, plan_change_scope must be high_level_replan.
- If replan.level is high_level, remaining_plan_still_valid must be false.
- If recommended_next_action is continue_current_plan or resequence_remaining_batches, remaining_plan_still_valid should normally be true.

Evidence usage rules:
- Base the decision on stage goals, batch outcomes, failed/partial/completed tasks, and recovery implications.
- Consider whether missing work is local and recoverable, or structural and planning-related.
- Distinguish between:
  - bad atomic decomposition
  - missing follow-up implementation
  - flawed high-level stage planning
  - unclear/unsafe state requiring human review
  - local recoverable context-resolution failure
- Consider whether a newly created recovery task is blocking or non-blocking in the context of the remaining plan.
- Consider whether leaving a single isolated recovery task for immediate execution would create an avoidable extra validation cycle.
- Consider whether regrouping that new work into later batches is more coherent than continuing unchanged.

Output quality rules:
- decision_summary must clearly explain the stage-level conclusion
- recommended_next_action_reason must explain WHY that next action is preferable over the nearby alternatives
- decision_signals must list the main operational factors
- evaluated_batches must summarize the meaningful outcomes of the processed batches
- key_risks should identify the main unresolved risks
- notes may include operational observations useful for downstream orchestration
- Do not include text outside the schema
""".strip()


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
    project_name: str,
    validation_error: str,
) -> str:
    return f"""
Project name: {project_name}

Your previous output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Critical corrections:
- do not use refined as a replan level
- only valid replan levels are atomic and high_level
- do not use retry_batch
- keep decision, project_stage_closed, manual_review_required, recovery_strategy, replan, and recommended_next_action fully consistent
- keep plan_change_scope, remaining_plan_still_valid, new_recovery_tasks_blocking, and single_task_tail_risk consistent with the recommended action
- do not set stage_completed unless the stage is truly closed
- do not request replan_from_high_level unless replan.required=true and replan.level=high_level
- do not request insert_followup_atomic_tasks unless followup_atomic_tasks_required=true
- do not request manual_review unless manual_review_required=true
- do not use replan_remaining_work unless replan.required=true and replan.level=high_level
- do not use continue_current_plan together with follow-up tasks, replanning, or manual review
- prefer the narrowest sufficient recovery action
- do not escalate a local recoverable context-selection failure to manual review unless the evidence clearly requires it
- return only JSON matching the schema
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
    strict_schema = to_openai_strict_json_schema(
        StageEvaluationOutput.model_json_schema()
    )

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
        json_schema=strict_schema,
    )

    try:
        return StageEvaluationOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_stage_evaluation_retry_prompt(
            project_name=project_name,
            validation_error=str(exc),
        )

        raw_retry = provider.generate_structured(
            system_prompt=STAGE_EVALUATION_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="stage_evaluation_output",
            json_schema=strict_schema,
        )

        return StageEvaluationOutput.model_validate(raw_retry)