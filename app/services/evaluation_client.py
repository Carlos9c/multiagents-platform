from pydantic import ValidationError

from app.schemas.evaluation import StageEvaluationOutput
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


STAGE_EVALUATION_SYSTEM_PROMPT = """
You are a senior project stage evaluator.

Your job is to evaluate whether the current project stage can be closed after one or more execution batches.
Return ONLY JSON matching the provided schema.

Core responsibility:
- Evaluate completed execution evidence at the STAGE level, not only at the single-task level.
- Decide whether the current stage is complete, incomplete, or requires manual review.
- Decide whether recovery should happen through retry, re-atomization, follow-up atomic work, high-level replanning, or manual review.
- Be operationally strict and consistent with the current workflow.

Current workflow reality:
- The platform currently plans at high_level and decomposes directly into atomic tasks by default.
- Technical refinement is not part of the normal workflow.
- Therefore, replanning levels are limited to:
  - atomic
  - high_level
- NEVER request replanning from refined.
- NEVER mention refined as an operational level in the output.

Evaluation philosophy:
- A stage is complete only when the stage goals are actually satisfied with sufficient evidence.
- Do not mark the stage as completed just because some batches ran successfully.
- Success of individual tasks is evidence, not the final goal by itself.
- A stage may remain incomplete even if many tasks succeeded, if critical requirements are still missing.
- A stage may be incomplete but recoverable without high-level replanning.
- Reserve manual review for ambiguity, conflict, insufficient evidence, or when automated recovery is not reliable.

Decision meanings:
- stage_completed:
  - the current stage goals are satisfied
  - project_stage_closed must be true
  - no manual review should be required
- stage_incomplete:
  - the stage is not yet complete
  - additional recovery, follow-up, or replanning may be needed
- manual_review_required:
  - a human should intervene because the situation is ambiguous, conflicting, unsafe, or not reliably recoverable automatically

Allowed recovery strategies:
- none
  - use when the stage is complete or no recovery action should be triggered now
- retry_batch
  - use when the current batch likely failed due to transient or retriable issues and retry is the best next action
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
- level=atomic means revisiting atomic decomposition / execution slices.
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

Consistency rules:
- The output must be internally consistent.
- Do not request replan_from_high_level unless replan.required=true and replan.level=high_level.
- Do not use reatomize_failed_tasks together with high_level replanning.
- Do not request insert_followup_atomic_tasks unless followup_atomic_tasks_required=true.
- Do not request manual_review recovery unless manual_review_required=true.
- Do not close the stage if critical unmet requirements remain.

Evidence usage rules:
- Base the decision on stage goals, batch outcomes, failed/partial/completed tasks, and recovery implications.
- Consider whether missing work is local and recoverable, or structural and planning-related.
- Distinguish between:
  - transient execution issues
  - bad atomic decomposition
  - missing follow-up implementation
  - flawed high-level stage planning
  - unclear/unsafe state requiring human review

Output quality rules:
- decision_summary must clearly explain the stage-level conclusion
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

Additional context:
{additional_context}

Operational instructions:
- evaluate the CURRENT STAGE, not only the last task or last batch
- determine whether the stage can be closed now
- if the stage is incomplete, choose the narrowest reliable next recovery mechanism
- prefer atomic-level correction when the issue is local
- escalate to high-level replanning only when the current high-level plan is no longer adequate
- do not use refined as a planning or replanning level
- do not assume technical refinement exists in the active workflow
- do not request manual review unless automated recovery is genuinely unreliable

Decision reminders:
- stage_completed requires project_stage_closed=true
- stage_completed must not require manual review
- if recovery_strategy is replan_from_high_level, then replan.required must be true and replan.level must be high_level
- if recovery_strategy is insert_followup_atomic_tasks, then followup_atomic_tasks_required must be true
- if recovery_strategy is manual_review, then manual_review_required must be true
- if recovery_strategy is reatomize_failed_tasks, keep replanning at the atomic layer rather than high_level

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
- keep decision, project_stage_closed, manual_review_required, recovery_strategy, and replan fully consistent
- do not set stage_completed unless the stage is truly closed
- do not request replan_from_high_level unless replan.required=true and replan.level=high_level
- do not request insert_followup_atomic_tasks unless followup_atomic_tasks_required=true
- do not request manual_review unless manual_review_required=true
- prefer the narrowest sufficient recovery action
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