from __future__ import annotations

from app.services.validation.contracts import TaskValidationInput
from app.services.validation.validators.code.renderer import (
    CodeValidationRenderableEvidence,
)


CODE_TASK_VALIDATOR_SYSTEM_PROMPT = """
You are a senior code-task validation agent.

Your job is to validate whether an executed task is actually complete, partial, failed, or requires manual review.

You MUST reason from:
- the task definition
- the execution outcome
- the request context
- the rendered validation evidence provided to you

You are NOT the executor.
You are NOT the router.
You are the validator.

You must determine:
- whether the task objective appears satisfied
- whether the acceptance criteria appear satisfied
- whether the evidence supports the claimed completed scope
- whether meaningful remaining scope still exists
- whether blockers or contradictions prevent closure
- whether the result should be classified as completed, partial, failed, or manual_review

Critical rules:
- Do not assume the task is complete just because files changed.
- Do not assume the task is failed just because execution had trouble; inspect the actual evidence.
- Only reason over the evidence that has been rendered for this validator.
- If evidence is insufficient to validate reliably, choose manual_review.
- If the task appears partly implemented but incomplete, choose partial.
- If the evidence strongly contradicts the claimed completion, choose failed.
- findings must be specific and grounded in the provided evidence.
- evidence_refs should reference things like:
  - produced_file:<path>
  - command:<index>
  - artifact:<id_or_ref>

Return ONLY JSON matching the provided schema.
""".strip()


def build_code_task_validator_user_prompt(
    *,
    validation_input: TaskValidationInput,
    renderable_evidence: CodeValidationRenderableEvidence,
) -> str:
    task = validation_input.task
    execution = validation_input.execution
    request_context = validation_input.request_context

    unsupported_ids = [
        item.evidence_id for item in renderable_evidence.unsupported_items
    ]

    return f"""
Validate this task execution result.

Task:
- id: {task.task_id}
- title: {task.title}
- description: {task.description}
- summary: {task.summary}
- objective: {task.objective}
- acceptance_criteria: {task.acceptance_criteria}
- technical_constraints: {task.technical_constraints}
- out_of_scope: {task.out_of_scope}
- task_type: {task.task_type}
- planning_level: {task.planning_level}

Execution outcome:
- execution_run_id: {execution.execution_run_id}
- execution_status: {execution.execution_status}
- decision: {execution.decision}
- summary: {execution.summary}
- details: {execution.details}
- rejection_reason: {execution.rejection_reason}
- completed_scope: {execution.completed_scope}
- remaining_scope: {execution.remaining_scope}
- blockers_found: {execution.blockers_found}
- validation_notes: {execution.validation_notes}
- output_snapshot: {execution.output_snapshot}
- execution_agent_sequence: {execution.execution_agent_sequence}

Request context:
- allowed_paths: {request_context.allowed_paths}
- relevant_files: {request_context.relevant_files}
- key_decisions: {request_context.key_decisions}
- related_task_ids: {request_context.related_task_ids}

Unsupported evidence items not rendered to this validator:
{unsupported_ids}

Rendered evidence for this validator:
{renderable_evidence.rendered_text}

Instructions:
- Validate only what can be supported by the rendered evidence.
- If unsupported evidence items appear necessary to fully validate the task, choose partial or manual_review as appropriate.
- Explain clearly what was validated and what remains unvalidated.
- Keep findings evidence-based and specific.
""".strip()
