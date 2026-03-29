from __future__ import annotations

import json

from pydantic import ValidationError

from app.schemas.recovery_assignment import (
    RecoveryAssignmentInput,
    RecoveryAssignmentLLMOutput,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema

RECOVERY_ASSIGNMENT_SYSTEM_PROMPT = """
You are the recovery assignment planner for a live multi-batch execution plan.

Your job is NOT to decide final batch ids or mutate the plan directly.
Your job is to interpret newly created recovery tasks and produce a structured assignment proposal.

You must do three things:
1. assess each new task individually
2. group the new tasks into execution clusters
3. classify each cluster by operational impact and placement relation

You are operating AFTER recovery and AFTER evaluation/post-batch resolution.
That means:
- the global action has already been resolved
- you must not re-litigate the whole project
- you must work within the resolved assignment mode unless structural conflict makes replan necessary

Important platform rules:
- You do NOT assign final batch ids.
- You do NOT invent patch batch ids.
- You do NOT rewrite the whole remaining plan.
- You do NOT leave any new task unaccounted for.
- Every new task must appear exactly once in task_assessments and exactly once inside exactly one cluster.
- The placement unit is the cluster, not the individual task.
- The order of tasks inside each cluster is the execution order from earliest to latest.
- That internal order must eliminate internal dependency ambiguity as much as possible.

What a cluster means:
- A cluster is a unit of assignment into the live plan.
- Tasks that depend on each other should normally be in the same cluster.
- Cross-cluster dependencies between new tasks are NOT allowed in this contract.
- If task A depends on new task B, both must be in the same cluster, and B must appear earlier in the cluster order.
- A cluster may contain only one task if grouping is unnecessary.

Impact types:
- immediate_blocking:
  the cluster must execute before the next useful progress can continue
- future_blocking:
  the cluster does not block the next useful progress, but must happen before a future consumer batch or future dependent work
- additive_deferred:
  the cluster adds useful work and can be deferred without blocking current immediate progress
- corrective_local:
  the cluster locally corrects or completes recent work, but is still locally manageable without full replan
- structural_conflict:
  the cluster reveals that the remaining plan is not structurally valid enough and requires replan

Placement relations:
- before_next_useful_progress:
  use for immediate_blocking clusters only
- before_first_consumer_batch:
  use for future_blocking clusters, and sometimes for corrective_local or additive_deferred when they clearly belong before future work
- after_current_tail:
  use for additive_deferred or corrective_local clusters that can safely be deferred to the tail
- requires_replan:
  use only for structural_conflict clusters

Strategy rules:
- Allowed strategy values:
  - continue_with_assignment
  - resequence_with_assignment
  - requires_replan
- Prefer the provided assignment_mode unless there is a genuine structural conflict.
- Do NOT escalate to requires_replan just because new tasks exist.
- Use requires_replan only when the new work reveals a structural contradiction or invalid remaining plan.
- If the input resolved_action is continue_current_plan, your default strategy should be continue_with_assignment.
- If the input resolved_action is resequence_remaining_batches, your default strategy should be resequence_with_assignment.

Clustering rules:
- Group tasks that form one local prerequisite chain.
- Group tasks that produce one coherent intermediate deliverable.
- Group tasks that should be assigned together to avoid ambiguous sequencing.
- Do NOT over-group unrelated work.
- Do NOT split obviously dependent new tasks into separate clusters.
- Avoid singleton clusters when there is a clear dependency chain that belongs together.
- Prefer compact, operationally coherent clusters.

Assessment rules:
- task_assessments must preserve per-task reasoning and dependency signals.
- Each task_assessment must reference a suggested_cluster_id that actually exists.
- impact_type at task level must match the impact_type of its cluster.
- depends_on_new_task_ids must list only new tasks inside the same cluster.
- If a task depends on another new task, that dependency task must appear earlier in the cluster execution order.
- depends_on_existing_task_ids may reference existing already-known tasks or pending tasks from the provided context.

Reasoning rules:
- Read the executed batch summary, evaluation signals, recovery signals, live plan summary, next useful progress, and known relationships together.
- Treat the input as a prepared operational briefing.
- Do not invent project facts not supported by the input.
- Use known relationships when present.
- Respect already-known internal dependency hints.
- Use parent task affinity, acceptance criteria, implementation notes, and known relationships to decide grouping.
- Consider both what has already been completed and what still remains in the live plan.

Important distinction:
- A cluster can be non-blocking now but still future_blocking.
- A corrective cluster is not automatically structural_conflict.
- New tasks do not automatically imply resequence or replan.
- A local correction can still be additive_deferred or corrective_local.
- Structural conflict is reserved for real contradictions with the remaining plan.

Output quality requirements:
- Return only valid JSON matching the schema.
- Cover every new task exactly once.
- Keep rationales concise but specific.
- Use stable, readable cluster ids such as "cluster_1", "cluster_2", etc.
- Ensure the order of tasks inside each cluster is executable earliest-to-latest.
""".strip()


def _pretty_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)


def build_recovery_assignment_user_prompt(
    assignment_input: RecoveryAssignmentInput,
) -> str:
    payload = assignment_input.model_dump(mode="json")

    return f"""
Plan a recovery assignment proposal for the provided live-plan situation.

Important:
- The global action has already been resolved.
- You must not assign final batch ids.
- You must propose clusters and placement relations only.
- Every new task must be covered exactly once.
- The order inside each cluster must be the execution order from earliest to latest.

Structured assignment input:
{_pretty_json(payload)}

Return only JSON matching the requested schema.
""".strip()


def build_recovery_assignment_retry_prompt(
    *,
    validation_error: str,
    assignment_input: RecoveryAssignmentInput,
) -> str:
    payload = assignment_input.model_dump(mode="json")

    return f"""
Your previous recovery assignment output was invalid.

Validation error:
{validation_error}

You must correct the output and return valid JSON matching the schema.

Critical reminders:
- Cover every new task exactly once.
- Every task_assessment.suggested_cluster_id must exist in clusters.
- task_assessments and clusters must cover the exact same task ids.
- No task may appear in more than one cluster.
- Cluster task order must be execution order, earliest to latest.
- If a task depends on another new task, both tasks must be in the same cluster and the dependency must appear earlier.
- Cross-cluster new-task dependencies are not allowed in this contract.
- impact_type must match between each task and its cluster.
- Use the resolved assignment mode unless structural conflict truly requires replan.
- Do not assign final batch ids.

Structured assignment input:
{_pretty_json(payload)}

Return only JSON matching the requested schema.
""".strip()


def call_recovery_assignment_model(
    *,
    assignment_input: RecoveryAssignmentInput,
) -> RecoveryAssignmentLLMOutput:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(RecoveryAssignmentLLMOutput.model_json_schema())

    first_user_prompt = build_recovery_assignment_user_prompt(
        assignment_input=assignment_input,
    )

    raw = provider.generate_structured(
        system_prompt=RECOVERY_ASSIGNMENT_SYSTEM_PROMPT,
        user_prompt=first_user_prompt,
        schema_name="recovery_assignment_output",
        json_schema=strict_schema,
    )

    try:
        return RecoveryAssignmentLLMOutput.model_validate(raw)
    except ValidationError as exc:
        retry_user_prompt = build_recovery_assignment_retry_prompt(
            validation_error=str(exc),
            assignment_input=assignment_input,
        )

        raw_retry = provider.generate_structured(
            system_prompt=RECOVERY_ASSIGNMENT_SYSTEM_PROMPT,
            user_prompt=retry_user_prompt,
            schema_name="recovery_assignment_output",
            json_schema=strict_schema,
        )

        return RecoveryAssignmentLLMOutput.model_validate(raw_retry)
