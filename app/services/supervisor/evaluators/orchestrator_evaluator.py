"""
Supervisor evaluator for the execution orchestrator.

One LLM evaluation call per project.  Analyses execution_trace.jsonl entries
for the orchestrator (up to 15 runs) to assess decision-making quality:
invalid/normalized decision rates, forced-terminal rate, subagent sequence
appropriateness, and budget-exhaustion patterns.
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
    _MAX_RUNS_PER_EVALUATION,
    load_execution_trace_entries,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

ORCHESTRATOR_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("orchestrator_evaluator")

_MAX_TRACE_RUNS = _MAX_RUNS_PER_EVALUATION


def _select_runs(entries: list[dict]) -> list[dict]:
    """Prioritise failed/rejected runs before applying the cap."""
    priority = [
        e
        for e in entries
        if e.get("final_decision") in {"reject", "env_manager_rejected", "budget_exceeded"}
    ]
    rest = [
        e
        for e in entries
        if e.get("final_decision") not in {"reject", "env_manager_rejected", "budget_exceeded"}
    ]
    combined = priority + rest
    return combined[:_MAX_TRACE_RUNS]


def _build_user_prompt(
    *,
    project_id: int,
    system_prompt: str,
    runs: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "orchestrator_evaluator",
        "main",
        {
            "project_id": project_id,
            "system_prompt": system_prompt,
            "runs": runs,
            "total_run_count": len(runs),
        },
    )
    return f"""
Evaluate the execution orchestrator's decision-making quality for this project.

Project ID: {project_id}

System prompt that was active during orchestration:
---
{system_prompt}
---

Orchestrator trace entries ({len(runs)} runs):
{json.dumps(runs, ensure_ascii=False, indent=2)}

Instructions:
- Assess overall decision quality across all runs: invalid_decision_count,
  normalized_decision_count, and the rate of forced_terminal=true decisions.
- Identify whether final_decision distributions (finish/reject/budget_exceeded/
  env_manager_rejected) suggest systematic problems or healthy variation.
- For each run, check whether the execution_agent_sequence is appropriate for
  the task type implied by the run data.
- Note any runs with high repair_attempts_used that also ended in reject or
  budget_exceeded — these signal loops where the orchestrator could not converge.
- Provide a verdict with specific references to the trace data.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "orchestrator_evaluator",
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


class OrchestratorEvaluator:
    """Evaluates the execution orchestrator for a project."""

    AGENT_NAME = "orchestrator"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        all_entries = load_execution_trace_entries(project_id, agent="orchestrator")

        if not all_entries:
            return EvaluatorOutput(result=None)

        runs = _select_runs(all_entries)
        system_prompt = resolve_system_prompt(
            "orchestrator", "decision", system_version=system_version
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            system_prompt=system_prompt,
            runs=runs,
        )

        raw = provider.generate_structured(
            system_prompt=ORCHESTRATOR_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="orchestrator_evaluator_output",
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
                system_prompt=ORCHESTRATOR_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="orchestrator_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
