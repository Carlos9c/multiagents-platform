from pydantic import ValidationError

from app.models.task import Task
from app.schemas.code_execution import CodeExecutorResult
from app.schemas.code_validation import (
    CodeValidationEvidence,
    CodeValidationFulfillmentDecision,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema


CODE_VALIDATOR_SYSTEM_PROMPT = """
You are a strict code task fulfillment validator.

Your only job is to decide whether the delivered result satisfies the requested task.

You must return ONLY JSON matching the provided schema.

Core rules:
- Decide only one of:
  - completed
  - partial
  - failed
- Focus on whether the delivered result satisfies the task as requested.
- Do NOT propose improvements.
- Do NOT suggest next steps.
- Do NOT judge style unless it directly prevents fulfillment.
- Use the task definition, resolved execution context, working set, edit plan, observed changes,
  workspace diff, final file snapshots, and project operational context.
- Be conservative.
- Use completed only when the delivered result substantially satisfies the task.
- Use partial when meaningful progress exists but relevant required parts remain unresolved.
- Use failed when the result does not satisfy the task in a meaningful way.

Critical validation behavior:
- Treat the resolved execution context as evidence, not as an excuse.
- Sparse context is not automatic failure.
- New-file creation can be valid if it is justified by the task.
- A good diff alone is not sufficient; the final result must satisfy the task objective and acceptance criteria.
- Use project operational context only as supporting continuity evidence, not as a replacement for judging the actual delivered result.
- If the final delivered result contradicts the task, fail or mark partial even if the planning looked reasonable.

Decision semantics:
- completed: the delivered resolution satisfies the task as requested
- partial: the delivered resolution satisfies only part of the task
- failed: the delivered resolution does not satisfy the task

Output discipline:
- decision_reason must be short and direct
- missing_requirements must list only unmet parts of the task
- evidence_used must list the key pieces of evidence used in the decision
""".strip()


def _truncate(text: str | None, limit: int = 12000) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _build_file_snapshots_text(file_snapshots: dict[str, str]) -> str:
    if not file_snapshots:
        return "No final file snapshots available."

    parts: list[str] = []
    for path, content in file_snapshots.items():
        parts.append(f"FILE: {path}")
        parts.append(_truncate(content, limit=8000) or "")
        parts.append("")
    return "\n".join(parts).strip()


def _build_project_memory_text(evidence: CodeValidationEvidence) -> str:
    memory = evidence.project_operational_context

    referenced_paths = ", ".join(item.path for item in memory.referenced_paths[:10]) or "none"
    active_workstreams = "; ".join(memory.active_workstreams[:6]) or "none"
    open_gaps = "; ".join(memory.open_gaps[:6]) or "none"
    recent_completed = "; ".join(memory.recent_completed_work[:6]) or "none"
    validation_learnings = "; ".join(memory.validation_learnings[:6]) or "none"
    recovery_learnings = "; ".join(memory.recovery_learnings[:6]) or "none"

    return f"""
Project operational context:
- summary: {memory.summary}
- active_workstreams: {active_workstreams}
- recent_completed_work: {recent_completed}
- open_gaps: {open_gaps}
- validation_learnings: {validation_learnings}
- recovery_learnings: {recovery_learnings}
- referenced_paths: {referenced_paths}
""".strip()


def _build_validation_user_prompt(
    task: Task,
    executor_result: CodeExecutorResult,
    evidence: CodeValidationEvidence,
) -> str:
    resolved_context = evidence.resolved_execution_context
    working_set = evidence.working_set
    edit_plan = evidence.edit_plan

    return f"""
Task:
- task_id: {task.id}
- title: {task.title}
- description: {task.description}
- objective: {task.objective}
- acceptance_criteria: {task.acceptance_criteria}
- technical_constraints: {task.technical_constraints}
- out_of_scope: {task.out_of_scope}

Resolved execution context:
- relevant_decisions: {resolved_context.relevant_decisions}
- candidate_modules: {resolved_context.candidate_modules}
- candidate_files: {resolved_context.candidate_files}
- primary_targets: {resolved_context.primary_targets}
- related_files: {resolved_context.related_files}
- reference_files: {resolved_context.reference_files}
- related_test_files: {resolved_context.related_test_files}
- relevant_symbols: {resolved_context.relevant_symbols}
- unresolved_questions: {resolved_context.unresolved_questions}
- selection_rationale: {resolved_context.selection_rationale}
- selection_confidence: {resolved_context.selection_confidence}

Working set actually used:
- target_files: {working_set.target_files}
- related_files: {working_set.related_files}
- reference_files: {working_set.reference_files}
- test_files: {working_set.test_files}
- repo_guidance: {working_set.repo_guidance}

Execution result:
- execution_status: {executor_result.execution_status}
- execution_summary: {executor_result.journal.summary}
- claimed_completed_scope: {executor_result.journal.claimed_completed_scope}
- claimed_remaining_scope: {executor_result.journal.claimed_remaining_scope}
- encountered_uncertainties: {executor_result.journal.encountered_uncertainties}
- notes_for_validator: {executor_result.journal.notes_for_validator}

Approved edit plan:
- summary: {edit_plan.summary}
- planned_changes: {[item.model_dump(mode="json") for item in edit_plan.planned_changes]}
- assumptions: {edit_plan.assumptions}
- local_risks: {edit_plan.local_risks}
- notes: {edit_plan.notes}

Observed validation evidence:
- checked_files: {evidence.checked_files}
- observed_changes: {evidence.observed_changes}
- executed_checks: {[item.model_dump(mode="json") for item in evidence.executed_checks]}
- check_outputs: {evidence.check_outputs}
- warnings: {evidence.warnings}

Workspace diff:
{_truncate(evidence.workspace_diff, limit=15000) or "No diff available."}

Final file snapshots:
{_build_file_snapshots_text(evidence.final_file_snapshots)}

{_build_project_memory_text(evidence)}

Your task:
Decide only whether the delivered result satisfies the task request.
Use the resolved execution context and project memory as supporting evidence.
Do not fail automatically because context was sparse.
Judge the final delivered work against the requested task.
Return only the strict JSON decision.
""".strip()


class CodeValidatorClientError(Exception):
    """Base exception for code validator client errors."""


def evaluate_code_task_fulfillment(
    task: Task,
    executor_result: CodeExecutorResult,
    evidence: CodeValidationEvidence,
) -> CodeValidationFulfillmentDecision:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(
        CodeValidationFulfillmentDecision.model_json_schema()
    )

    user_prompt = _build_validation_user_prompt(
        task=task,
        executor_result=executor_result,
        evidence=evidence,
    )

    raw = provider.generate_structured(
        system_prompt=CODE_VALIDATOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="code_validation_fulfillment_decision",
        json_schema=strict_schema,
    )

    try:
        return CodeValidationFulfillmentDecision.model_validate(raw)
    except ValidationError as exc:
        raise CodeValidatorClientError(
            f"Invalid structured response from code fulfillment validator model: {str(exc)}"
        ) from exc