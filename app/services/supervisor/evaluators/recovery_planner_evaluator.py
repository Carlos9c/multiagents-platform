"""
Supervisor evaluator for the recovery_planner agent.

Assesses action calibration against source run failure evidence, confidence
calibration, quality of created_tasks scope, still_blocks_progress accuracy,
and escalation rate to manual_review.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.evaluators._execution_helpers import get_system_versions_for_runs
from app.services.supervisor.evaluators._recovery_evaluation_helpers import (
    load_recovery_decision_artifacts,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

RECOVERY_PLANNER_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("recovery_planner_evaluator")

_AGENT_NAME = "recovery_planner"


def _build_user_prompt(
    *,
    project_id: int,
    agent_system_prompt: str,
    decisions: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "recovery_planner_evaluator",
        "main",
        {
            "project_id": project_id,
            "agent_system_prompt": agent_system_prompt,
            "decisions": decisions,
            "total_decision_count": len(decisions),
        },
    )
    return f"""
Evaluate the recovery_planner's decision quality for project {project_id}.

System prompt that was active during recovery:
---
{agent_system_prompt}
---

Recovery decisions analysed ({len(decisions)} decisions):
{json.dumps(decisions, ensure_ascii=False, indent=2)}

For each decision, source_run_context provides the execution failure evidence (cross-context),
and source_task_context provides the source task's type, objective, and status.
The system invariant: if source_task_context.status=failed and action=insert_followup,
the system automatically promoted the action to reatomize — treat this as a calibration failure.

Assess action calibration, confidence calibration, created_tasks quality, still_blocks_progress
accuracy, and manual_review escalation appropriateness.
Reference specific artifact_id, source_task_id, and source_run_id when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "recovery_planner_evaluator",
        "retry",
        {
            "project_id": project_id,
            "validation_error": validation_error,
        },
    )
    return f"""
Your previous response for project_id={project_id} was invalid.

Validation error:
{validation_error}

Return valid JSON with all required fields:
- verdict: one of "healthy", "needs_attention", or "degraded"
- findings: prose description (minimum 20 characters)
- issues: list of specific problems (empty list if none)
- suggestions: list of actionable suggestions (empty list if none)
""".strip()


class RecoveryPlannerEvaluator:
    """Evaluates the recovery_planner's decision quality for a project."""

    AGENT_NAME = _AGENT_NAME

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        decisions = load_recovery_decision_artifacts(db, project_id)

        if not decisions:
            return EvaluatorOutput(result=None)

        run_ids = sorted(
            {
                d["source_run_context"]["run_id"]
                for d in decisions
                if d.get("source_run_context") and d["source_run_context"].get("run_id")
            }
        )
        system_versions = get_system_versions_for_runs(db, run_ids)

        agent_system_prompt = resolve_system_prompt(_AGENT_NAME, system_version=system_version)
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            agent_system_prompt=agent_system_prompt,
            decisions=decisions,
        )

        raw = provider.generate_structured(
            system_prompt=RECOVERY_PLANNER_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="recovery_planner_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            retry_prompt = _build_retry_prompt(
                project_id=project_id,
                validation_error=str(exc),
            )
            raw_retry = provider.generate_structured(
                system_prompt=RECOVERY_PLANNER_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="recovery_planner_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(
            result=result, execution_run_ids_analyzed=run_ids, system_versions_seen=system_versions
        )
