from pydantic import ValidationError

from app.models.execution_run import ExecutionRun
from app.models.task import Task
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema
from pydantic import BaseModel, Field


RECOVERY_ACTION_RETRY = "retry"
RECOVERY_ACTION_REATOMIZE = "reatomize"
RECOVERY_ACTION_INSERT_FOLLOWUP = "insert_followup"
RECOVERY_ACTION_MANUAL_REVIEW = "manual_review"

VALID_RECOVERY_ACTIONS = {
    RECOVERY_ACTION_RETRY,
    RECOVERY_ACTION_REATOMIZE,
    RECOVERY_ACTION_INSERT_FOLLOWUP,
    RECOVERY_ACTION_MANUAL_REVIEW,
}


class RecoveryDecisionTaskCreate(BaseModel):
    title: str
    description: str
    objective: str | None = None
    implementation_notes: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    task_type: str = "implementation"
    priority: str = "medium"
    executor_type: str = "code_executor"


class RecoveryDecision(BaseModel):
    action: str
    reason: str
    created_tasks: list[RecoveryDecisionTaskCreate] = Field(default_factory=list)
    retry_same_task: bool = False
    requires_manual_review: bool = False


RECOVERY_SYSTEM_PROMPT = """
You are a strict recovery decision agent for a multi-step code execution system.

You receive:
- the current task
- the latest execution run
- execution context summary
- validation context summary
- the next batch summary
- the remaining execution plan summary

Your job is to decide the best recovery action for the task.

Allowed actions:
- retry
- reatomize
- insert_followup
- manual_review

Critical rules:
- Validation context is mandatory evidence for deciding recovery quality.
- Use execution context to understand what was attempted.
- Use validation context to understand why the task did not satisfy what was requested.
- Prefer reatomize if the task is too broad, ambiguous, or structurally wrong.
- Prefer insert_followup if the task mostly makes sense but now needs one or more concrete follow-up atomic tasks.
- Prefer retry only if the task is still correct as defined and another attempt is reasonable.
- Prefer manual_review if the situation is too ambiguous or risky.

Output discipline:
- Return ONLY JSON matching the schema.
- Be concrete.
- Do not propose vague suggestions.
"""


def _build_recovery_user_prompt(
    *,
    task: Task,
    run: ExecutionRun,
    next_batch_summary: str | None,
    remaining_plan_summary: str | None,
    execution_context_summary: str,
    validation_context_summary: str,
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
- current_task_status: {task.status}
- planning_level: {task.planning_level}
- executor_type: {task.executor_type}

Latest run:
- run_id: {run.id}
- run_status: {run.status}
- failure_type: {run.failure_type}
- failure_code: {run.failure_code}
- work_summary: {run.work_summary}
- work_details: {run.work_details}
- completed_scope: {run.completed_scope}
- remaining_scope: {run.remaining_scope}
- blockers_found: {run.blockers_found}
- validation_notes: {run.validation_notes}

Execution context summary:
{execution_context_summary}

Validation context summary:
{validation_context_summary}

Next batch summary:
{next_batch_summary or "None"}

Remaining execution plan summary:
{remaining_plan_summary or "None"}

Decide the best recovery action.
Return only strict JSON.
""".strip()


class RecoveryClientError(Exception):
    """Base exception for recovery client."""


def evaluate_recovery_decision(
    *,
    task: Task,
    run: ExecutionRun,
    next_batch_summary: str | None,
    remaining_plan_summary: str | None,
    execution_context_summary: str,
    validation_context_summary: str,
) -> RecoveryDecision:
    provider = get_llm_provider()
    strict_schema = to_openai_strict_json_schema(RecoveryDecision.model_json_schema())

    user_prompt = _build_recovery_user_prompt(
        task=task,
        run=run,
        next_batch_summary=next_batch_summary,
        remaining_plan_summary=remaining_plan_summary,
        execution_context_summary=execution_context_summary,
        validation_context_summary=validation_context_summary,
    )

    raw = provider.generate_structured(
        system_prompt=RECOVERY_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="recovery_decision",
        json_schema=strict_schema,
    )

    try:
        decision = RecoveryDecision.model_validate(raw)
    except ValidationError as exc:
        raise RecoveryClientError(
            f"Invalid structured response from recovery model: {str(exc)}"
        ) from exc

    if decision.action not in VALID_RECOVERY_ACTIONS:
        raise RecoveryClientError(
            f"Unsupported recovery action '{decision.action}'. "
            f"Allowed actions: {sorted(VALID_RECOVERY_ACTIONS)}"
        )

    return decision