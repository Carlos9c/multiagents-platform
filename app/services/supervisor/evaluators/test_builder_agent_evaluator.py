"""
Supervisor evaluator for the test_builder_agent (executor side of the pair).

Evaluates test generation patterns: file organisation, scope coverage against
acceptance_criteria, and retry improvement.
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

TEST_BUILDER_AGENT_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("test_builder_agent_evaluator")

_AGENT_NAME = "test_builder_agent"
_VALIDATOR_NAME = "test_builder_agent_validator"


def _build_user_prompt(
    *,
    project_id: int,
    agent_system_prompt: str,
    tasks: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "test_builder_agent_evaluator",
        "main",
        {
            "project_id": project_id,
            "agent_system_prompt": agent_system_prompt,
            "tasks": tasks,
            "total_task_count": len(tasks),
        },
    )
    return f"""
Evaluate the test_builder_agent's test generation patterns for project {project_id}.

System prompt that was active during execution:
---
{agent_system_prompt}
---

Tasks analysed ({len(tasks)} tasks):
{json.dumps(tasks, ensure_ascii=False, indent=2)}

Each run contains: work_summary, completed_scope, changed_files (paths only),
blockers_found, joint_validation_decision, validator_result.decision/summary (cross-context).

Assess test file organisation, scope coverage against acceptance_criteria,
and retry improvement when the validator flagged gaps.
Do NOT assess test quality or assertion logic.
Reference specific run_id and task_title values when raising issues.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "test_builder_agent_evaluator",
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


class TestBuilderAgentEvaluator:
    """Evaluates the test_builder_agent's behavioral patterns for a project."""

    AGENT_NAME = _AGENT_NAME
    VALIDATOR_NAME = _VALIDATOR_NAME

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

        agent_system_prompt = resolve_system_prompt(_AGENT_NAME, system_version=system_version)
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            agent_system_prompt=agent_system_prompt,
            tasks=ctx["tasks"],
        )

        raw = provider.generate_structured(
            system_prompt=TEST_BUILDER_AGENT_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="test_builder_agent_evaluator_output",
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
                system_prompt=TEST_BUILDER_AGENT_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="test_builder_agent_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        system_versions = get_system_versions_for_runs(db, run_ids)
        return EvaluatorOutput(
            result=result, execution_run_ids_analyzed=run_ids, system_versions_seen=system_versions
        )
