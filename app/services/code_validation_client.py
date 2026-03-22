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

Your only job is to decide whether the produced resolution satisfies the requested task.

You must return ONLY JSON matching the provided schema.

Critical rules:
- Decide only one of:
  - completed
  - partial
  - failed
- Focus only on whether the delivered resolution satisfies the task as requested.
- Do NOT propose improvements.
- Do NOT give coding advice.
- Do NOT suggest next steps.
- Do NOT evaluate style unless it directly prevents the task from being satisfied.
- Use the task objective, acceptance criteria, constraints, out-of-scope, execution evidence,
  edit plan, diff, and final file contents to make your decision.
- Be conservative. If key required parts are missing, use partial or failed.
- Use completed only if the resolution substantially satisfies what the task asked for.
- Use partial if the resolution covers some meaningful portion but leaves relevant requested
  parts unresolved.
- Use failed if the resolution does not satisfy the task in a meaningful way.

Decision semantics:
- completed: the delivered resolution satisfies the task as requested
- partial: the delivered resolution satisfies only part of the task
- failed: the delivered resolution does not satisfy the task

Output discipline:
- decision_reason must be short and direct
- missing_requirements must list only unmet parts of the task
- evidence_used must list the key pieces of evidence used in the decision
"""


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


def _build_validation_user_prompt(
    task: Task,
    executor_result: CodeExecutorResult,
    evidence: CodeValidationEvidence,
) -> str:
    return f"""
Task:
- task_id: {task.id}
- title: {task.title}
- description: {task.description}
- objective: {task.objective}
- acceptance_criteria: {task.acceptance_criteria}
- technical_constraints: {task.technical_constraints}
- out_of_scope: {task.out_of_scope}

Execution result:
- execution_status: {executor_result.execution_status}
- execution_summary: {executor_result.journal.summary}
- claimed_completed_scope: {executor_result.journal.claimed_completed_scope}
- claimed_remaining_scope: {executor_result.journal.claimed_remaining_scope}
- encountered_uncertainties: {executor_result.journal.encountered_uncertainties}
- notes_for_validator: {executor_result.journal.notes_for_validator}

Edit plan:
- summary: {executor_result.edit_plan.summary}
- planned_changes: {[item.model_dump(mode="json") for item in executor_result.edit_plan.planned_changes]}
- assumptions: {executor_result.edit_plan.assumptions}
- local_risks: {executor_result.edit_plan.local_risks}
- notes: {executor_result.edit_plan.notes}

Observed validation evidence:
- checked_files: {evidence.checked_files}
- observed_changes: {evidence.observed_changes}
- executed_checks: {evidence.executed_checks}
- check_outputs: {evidence.check_outputs}
- warnings: {evidence.warnings}

Workspace diff:
{_truncate(evidence.workspace_diff, limit=15000) or "No diff available."}

Final file snapshots:
{_build_file_snapshots_text(evidence.final_file_snapshots)}

Your task:
Decide only whether the delivered resolution satisfies the task request.
Do not propose improvements.
Do not suggest what to do next.
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