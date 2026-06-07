"""
Supervisor evaluator for the review episode pipeline.

Covers three agents acting in sequence within a single review episode:
  - review_evaluator     (gathers clarification, proposes action plan)
  - confirmation_evaluator (interprets user confirmation)
  - impact_assessment_agent (classifies scope and specifies task mutations)

Assesses review efficiency, abandonment rate, impact scope calibration,
and impact reasoning quality.
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
    load_review_episode_artifacts,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

REVIEW_EPISODE_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("review_episode_evaluator")

# This evaluator covers three agents — use a descriptive identifier, not a single agent name.
_EVALUATOR_NAME = "review_episode"
_REVIEW_EVALUATOR_AGENT = "review_evaluator"
_CONFIRMATION_EVALUATOR_AGENT = "confirmation_evaluator"
_IMPACT_ASSESSMENT_AGENT = "impact_assessment_agent"


def _build_user_prompt(
    *,
    project_id: int,
    review_evaluator_system_prompt: str,
    confirmation_evaluator_system_prompt: str,
    impact_assessment_system_prompt: str,
    episodes: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "review_episode_evaluator",
        "main",
        {
            "project_id": project_id,
            "review_evaluator_system_prompt": review_evaluator_system_prompt,
            "confirmation_evaluator_system_prompt": confirmation_evaluator_system_prompt,
            "impact_assessment_system_prompt": impact_assessment_system_prompt,
            "episodes": episodes,
            "total_episode_count": len(episodes),
        },
    )
    return f"""
Evaluate the review episode pipeline quality for project {project_id}.

System prompts that were active during review episodes:

review_evaluator system prompt:
---
{review_evaluator_system_prompt}
---

confirmation_evaluator system prompt:
---
{confirmation_evaluator_system_prompt}
---

impact_assessment_agent system prompt:
---
{impact_assessment_system_prompt}
---

Review episodes recorded ({len(episodes)} episodes):
{json.dumps(episodes, ensure_ascii=False, indent=2)}

Each episode contains: episode_index, is_project_level, blocked_task_id, blocked_task_title,
review_attempts, proposed_plan, outcome ("confirmed" | "abandoned" | "disruptive_restart"),
impact_scope ("narrow" | "moderate" | "disruptive" | null), tasks_modified_count,
tasks_added_count, tasks_eliminated_count, tasks_superseded_count, impact_reasoning.

Field semantics:
- tasks_modified_count: existing tasks updated (description, criteria, dependencies)
- tasks_added_count: new atomic tasks created via the atomizer (new work blocks)
- tasks_eliminated_count: tasks cancelled by user decision, including cascade-cancelled parents
- tasks_superseded_count: tasks replaced by a full disruptive replan (ProjectStartService)

Assess review efficiency (review_attempts), abandonment rate, impact_scope calibration
vs tasks_modified/added/eliminated/superseded counts, and impact_reasoning quality.
A well-calibrated moderate scope should have non-zero modifications, additions, or eliminations.
A narrow scope should have zero tasks_modified, zero tasks_added, and zero tasks_eliminated.
Reference specific artifact_id and episode_index values when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "review_episode_evaluator",
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


class ReviewEpisodeEvaluator:
    """Evaluates the review episode pipeline quality for a project."""

    AGENT_NAME = _EVALUATOR_NAME

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        episodes = load_review_episode_artifacts(db, project_id)

        if not episodes:
            return EvaluatorOutput(result=None)

        review_system_prompt = resolve_system_prompt(
            _REVIEW_EVALUATOR_AGENT, system_version=system_version
        )
        confirmation_system_prompt = resolve_system_prompt(
            _CONFIRMATION_EVALUATOR_AGENT, system_version=system_version
        )
        impact_system_prompt = resolve_system_prompt(
            _IMPACT_ASSESSMENT_AGENT, system_version=system_version
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            review_evaluator_system_prompt=review_system_prompt,
            confirmation_evaluator_system_prompt=confirmation_system_prompt,
            impact_assessment_system_prompt=impact_system_prompt,
            episodes=episodes,
        )

        raw = provider.generate_structured(
            system_prompt=REVIEW_EPISODE_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="review_episode_evaluator_output",
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
                system_prompt=REVIEW_EPISODE_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="review_episode_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
