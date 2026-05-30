"""
Supervisor evaluator for the recovery_assignment agent.

Assesses strategy concordance with input intent, impact_type appropriateness,
cluster composition quality, rationale specificity, and compilation outcomes.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.evaluators._recovery_evaluation_helpers import (
    load_recovery_assignment_trios,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

RECOVERY_ASSIGNMENT_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("recovery_assignment_evaluator")

_AGENT_NAME = "recovery_assignment"


def _build_user_prompt(
    *,
    project_id: int,
    agent_system_prompt: str,
    trios: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "recovery_assignment_evaluator",
        "main",
        {
            "project_id": project_id,
            "agent_system_prompt": agent_system_prompt,
            "trios": trios,
            "total_trio_count": len(trios),
        },
    )
    return f"""
Evaluate the recovery_assignment agent's cluster proposals for project {project_id}.

System prompt that was active during assignment:
---
{agent_system_prompt}
---

Recovery assignment trios analysed ({len(trios)} trios):
{json.dumps(trios, ensure_ascii=False, indent=2)}

Each trio contains: input (what was sent to the LLM), output (the LLM's cluster proposals),
and compiled_plan (the compiled result after plan patching).

Assess strategy concordance with resolved_intent_type, impact_type appropriateness,
cluster composition and internal dependency ordering, rationale specificity,
and compilation completeness (assigned_task_ids vs input new_tasks).
Reference specific trio_index and cluster_id when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "recovery_assignment_evaluator",
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


class RecoveryAssignmentEvaluator:
    """Evaluates the recovery_assignment agent's cluster proposals for a project."""

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
        trios = load_recovery_assignment_trios(db, project_id)

        if not trios:
            return EvaluatorOutput(result=None)

        agent_system_prompt = resolve_system_prompt(_AGENT_NAME, system_version=system_version)
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            agent_system_prompt=agent_system_prompt,
            trios=trios,
        )

        raw = provider.generate_structured(
            system_prompt=RECOVERY_ASSIGNMENT_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="recovery_assignment_evaluator_output",
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
                system_prompt=RECOVERY_ASSIGNMENT_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="recovery_assignment_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
