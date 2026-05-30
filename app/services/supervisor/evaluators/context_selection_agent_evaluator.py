"""
Supervisor evaluator for the context_selection_agent.

One LLM evaluation call per project.  Analyses execution_trace.jsonl entries
for context_selection_agent (up to 15 runs) to assess selection quality:
whether the agent selected the right historical tasks, whether it over- or
under-selected, and whether the reasons were specific.

Also builds a historical_file_index from the DB so the evaluator can tell
whether relevant tasks were available in the pool or absent from it entirely.
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
    build_historical_file_index,
    load_execution_trace_entries,
)
from app.services.supervisor.prompt_resolver import resolve_system_prompt

logger = logging.getLogger(__name__)

CONTEXT_SELECTION_AGENT_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get(
    "context_selection_agent_evaluator"
)

_MAX_TRACE_RUNS = _MAX_RUNS_PER_EVALUATION


def _select_runs(entries: list[dict]) -> list[dict]:
    """Prioritise active selection runs (call_type=initial) over skipped ones."""
    active = [e for e in entries if e.get("call_type") == "initial"]
    skipped = [e for e in entries if e.get("call_type") != "initial"]
    combined = active + skipped
    return combined[:_MAX_TRACE_RUNS]


def _build_user_prompt(
    *,
    project_id: int,
    system_prompt: str,
    runs: list[dict],
    historical_file_index: dict,
) -> str:
    prompt_loader.validate_builder_inputs(
        "context_selection_agent_evaluator",
        "main",
        {
            "project_id": project_id,
            "system_prompt": system_prompt,
            "runs": runs,
            "total_run_count": len(runs),
            "historical_file_index": historical_file_index,
        },
    )
    return f"""
Evaluate the context_selection_agent's historical task selection quality for this project.

Project ID: {project_id}

System prompt that was active during context selection:
---
{system_prompt}
---

Context selection trace entries ({len(runs)} runs):
{json.dumps(runs, ensure_ascii=False, indent=2)}

Historical file index (file_path → [task_ids that changed it]):
{json.dumps(historical_file_index, ensure_ascii=False, indent=2)}

Instructions:
- For each run with call_type=initial, assess whether selected_tasks are genuinely
  relevant to the current task's objective and relevant_files.
- Use historical_file_index to check if files referenced in the current task's
  relevant_files were also changed by tasks NOT in candidate_titles. If so, those
  tasks were not available to the agent — do not penalise for not selecting them.
- Evaluate whether selection was proportionate: neither consistently over-selecting
  (selecting everything) nor under-selecting (selecting nothing with a full catalog).
- Assess whether selection reasons are specific and concrete or generic.
- Runs with call_type=skipped mean no candidates existed — this is correct behaviour.
- Provide a verdict with specific references to selected_count, candidate_count,
  and selection_reason values from the trace.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "context_selection_agent_evaluator",
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


class ContextSelectionAgentEvaluator:
    """Evaluates the context_selection_agent for a project."""

    AGENT_NAME = "context_selection_agent"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        all_entries = load_execution_trace_entries(project_id, agent="context_selection_agent")

        if not all_entries:
            return EvaluatorOutput(result=None)

        runs = _select_runs(all_entries)
        historical_file_index = build_historical_file_index(db, project_id)
        system_prompt = resolve_system_prompt(
            "context_selection_agent", system_version=system_version
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            system_prompt=system_prompt,
            runs=runs,
            historical_file_index=historical_file_index,
        )

        raw = provider.generate_structured(
            system_prompt=CONTEXT_SELECTION_AGENT_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="context_selection_agent_evaluator_output",
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
                system_prompt=CONTEXT_SELECTION_AGENT_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="context_selection_agent_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
