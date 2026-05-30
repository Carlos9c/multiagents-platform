"""
Supervisor evaluator for the environment_manager_agent.

One LLM evaluation call per project.  Looks at all ExecutionRun rows where
environment_manager_agent appeared in execution_agent_sequence (up to 15,
prioritising failed/manual_review runs).

If the agent never ran in this project, returns healthy with an explanatory
note — absence of environment install calls is itself a healthy signal.
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
    get_project_execution_runs_with_tasks,
    get_system_versions_for_runs,
    select_runs_for_evaluation,
    serialize_run_for_evaluator,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

ENVIRONMENT_MANAGER_AGENT_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get(
    "environment_manager_agent_evaluator"
)

_AGENT_NAME = "environment_manager_agent"
_PREFER_STATUSES = ["failed", "manual_review"]


def _build_user_prompt(
    *,
    project_id: int,
    system_prompt: str,
    runs: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "environment_manager_agent_evaluator",
        "main",
        {
            "project_id": project_id,
            "system_prompt": system_prompt,
            "runs": runs,
            "total_run_count": len(runs),
        },
    )
    return f"""
Evaluate the environment_manager_agent's incremental package installation quality for this project.

Project ID: {project_id}

System prompt that was active during environment management:
---
{system_prompt}
---

Execution runs where environment_manager_agent participated ({len(runs)} runs):
{json.dumps(runs, ensure_ascii=False, indent=2)}

Instructions:
- For each run, assess whether the environment_manager_agent successfully installed
  the required packages (run.status=completed or partial) or rejected (status=failed
  with ENV_INSTALL_FAILED in blockers_found).
- Identify whether failures were due to truly unavailable packages (acceptable) or
  packages that should have been in the base environment spec.
- Look for recurrent failures of the same package across multiple tasks — this
  indicates systematic under-specification of the base environment.
- Note when environment_manager_agent appears many times: frequent invocations mean
  the base RuntimeSpec is repeatedly insufficient.
- Provide a verdict with specific references to run_id, task_title, and blockers_found.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "environment_manager_agent_evaluator",
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

Correct the output and return valid JSON matching the schema.
Required fields: verdict (healthy|needs_attention|degraded), findings (string, min 20 chars), issues (list of strings), suggestions (list of strings).
""".strip()


class EnvironmentManagerAgentEvaluator:
    """Evaluates the environment_manager_agent for a project."""

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
        all_runs = get_project_execution_runs_with_tasks(db, project_id)
        selected = select_runs_for_evaluation(
            all_runs,
            agent_name=_AGENT_NAME,
            prefer_statuses=_PREFER_STATUSES,
        )

        if not selected:
            return EvaluatorOutput(result=None)

        run_ids = [r.id for r, _ in selected]
        system_versions = get_system_versions_for_runs(db, run_ids)
        runs = [serialize_run_for_evaluator(r, t) for r, t in selected]
        system_prompt = resolve_system_prompt(
            "environment_manager_agent", system_version=system_version
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            system_prompt=system_prompt,
            runs=runs,
        )

        raw = provider.generate_structured(
            system_prompt=ENVIRONMENT_MANAGER_AGENT_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="environment_manager_agent_evaluator_output",
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
                system_prompt=ENVIRONMENT_MANAGER_AGENT_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="environment_manager_agent_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(
            result=result, execution_run_ids_analyzed=run_ids, system_versions_seen=system_versions
        )
