"""
Supervisor evaluator for the requirements_evaluator agent.

Assesses question progression logic, termination timing, gathering efficiency,
draft quality trajectory, and question relevance across requirements evaluation calls.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.evaluators._conversation_evaluation_helpers import (
    load_requirements_evaluation_artifacts,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

REQUIREMENTS_EVALUATOR_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get(
    "requirements_evaluator_evaluator"
)

_AGENT_NAME = "requirements_evaluator"


def _build_user_prompt(
    *,
    project_id: int,
    agent_system_prompt: str,
    evaluations: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "requirements_evaluator_evaluator",
        "main",
        {
            "project_id": project_id,
            "agent_system_prompt": agent_system_prompt,
            "evaluations": evaluations,
            "total_evaluation_count": len(evaluations),
        },
    )
    return f"""
Evaluate the requirements_evaluator agent's gathering quality for project {project_id}.

System prompt that was active during requirements gathering:
---
{agent_system_prompt}
---

Requirements evaluation calls recorded ({len(evaluations)} calls):
{json.dumps(evaluations, ensure_ascii=False, indent=2)}

Each record contains: call_index, status ("needs_more" or "sufficient"), next_question,
reasoning, draft_snapshot (first 500 chars of the draft at that call), and
conversation_turn_count (messages exchanged up to that point).

Assess question progression, termination timing, efficiency, draft quality trajectory,
and question relevance. Reference specific call_index values when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "requirements_evaluator_evaluator",
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


class RequirementsEvaluatorEvaluator:
    """Evaluates the requirements_evaluator agent's gathering quality for a project."""

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
        evaluations = load_requirements_evaluation_artifacts(db, project_id)

        if not evaluations:
            return EvaluatorOutput(result=None)

        agent_system_prompt = resolve_system_prompt(_AGENT_NAME, system_version=system_version)
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            agent_system_prompt=agent_system_prompt,
            evaluations=evaluations,
        )

        raw = provider.generate_structured(
            system_prompt=REQUIREMENTS_EVALUATOR_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="requirements_evaluator_evaluator_output",
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
                system_prompt=REQUIREMENTS_EVALUATOR_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="requirements_evaluator_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
