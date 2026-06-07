"""
Supervisor evaluator for the image_generation_agent_validator (validator side of the pair).

Evaluates whether the validator correctly identified image quality issues and applied
consistent criteria across tasks.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.evaluators._execution_helpers import (
    get_system_versions_for_runs,
)
from app.services.supervisor.evaluators._pair_evaluation_helpers import (
    build_pair_evaluation_context,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

IMAGE_GENERATION_AGENT_VALIDATOR_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get(
    "image_generation_agent_validator_evaluator"
)

_AGENT_NAME = "image_generation_agent"
_VALIDATOR_NAME = "image_generation_agent_validator"


def _build_user_prompt(
    *,
    project_id: int,
    validator_system_prompt: str,
    pairs: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "image_generation_agent_validator_evaluator",
        "main",
        {
            "project_id": project_id,
            "validator_system_prompt": validator_system_prompt,
            "pairs": pairs,
            "total_pair_count": len(pairs),
        },
    )
    return f"""
Evaluate the image_generation_agent_validator's decision quality for project {project_id}.

Validator system prompt that was active during execution:
---
{validator_system_prompt}
---

Executor-validator pairs analysed ({len(pairs)} pairs):
{json.dumps(pairs, ensure_ascii=False, indent=2)}

Each pair contains: task_title, acceptance_criteria, generation output metadata,
and validation decision (completed/partial/failed/manual_review) with findings.

Assess consistency, calibration, and false positive/negative rates.
Reference specific run_id and task_title values when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "image_generation_agent_validator_evaluator",
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


class ImageGenerationAgentValidatorEvaluator:
    """Evaluates the image_generation_agent_validator's behavioral patterns for a project."""

    AGENT_NAME = _VALIDATOR_NAME

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        ctx = build_pair_evaluation_context(
            db,
            project_id,
            agent_name=_AGENT_NAME,
            validator_name=_VALIDATOR_NAME,
        )

        if not ctx["tasks"]:
            return EvaluatorOutput(result=None)

        validator_system_prompt = resolve_system_prompt(
            _VALIDATOR_NAME, system_version=system_version
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            validator_system_prompt=validator_system_prompt,
            pairs=ctx["tasks"],
        )

        raw = provider.generate_structured(
            system_prompt=IMAGE_GENERATION_AGENT_VALIDATOR_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="image_generation_agent_validator_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        run_ids = sorted({run["run_id"] for task in ctx["tasks"] for run in task.get("runs", [])})

        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            retry_prompt = _build_retry_prompt(
                project_id=project_id,
                validation_error=str(exc),
            )
            raw_retry = provider.generate_structured(
                system_prompt=IMAGE_GENERATION_AGENT_VALIDATOR_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="image_generation_agent_validator_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        system_versions = get_system_versions_for_runs(db, run_ids)
        return EvaluatorOutput(
            result=result,
            execution_run_ids_analyzed=run_ids,
            system_versions_seen=system_versions,
        )
