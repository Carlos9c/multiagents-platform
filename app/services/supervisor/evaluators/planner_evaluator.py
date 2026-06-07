"""
Supervisor evaluator for the planner agent.

Reads all planning_trace.jsonl entries with agent=planner for the project,
computes task outcome statistics from the DB, calls the LLM evaluator, and
returns an AgentEvaluationOutput.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_REATOMIZED,
    Task,
)
from app.services.llm.factory import get_llm_provider
from app.services.project_storage import ProjectStorageService
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.prompt_resolver import resolve_system_prompt
from app.services.supervisor.trace_writer import PLANNING_TRACE_FILENAME

logger = logging.getLogger(__name__)

PLANNER_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("planner_evaluator")

_storage = ProjectStorageService()


def _load_planner_trace_entries(project_id: int) -> list[dict]:
    """Read all planner entries from the project's planning_trace.jsonl."""
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME

    if not trace_path.exists():
        return []

    entries: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") == "planner":
                entries.append(entry)
        except json.JSONDecodeError:
            logger.warning(
                "planner_evaluator: invalid JSONL line in trace for project %s", project_id
            )
    return entries


def _compute_task_stats(db: Session, project_id: int) -> dict:
    """Return per-high-level-task stats for the evaluator prompt."""
    # High-level tasks (parent_task_id IS NULL, non-atomic)
    high_level_tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.parent_task_id.is_(None),
        )
        .all()
    )

    stats: list[dict] = []
    for hl_task in high_level_tasks:
        children = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.parent_task_id == hl_task.id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
            )
            .all()
        )
        total = len(children)
        completed = sum(1 for t in children if t.status == TASK_STATUS_COMPLETED)
        reatomized = sum(1 for t in children if t.status == TASK_STATUS_REATOMIZED)
        # Cancelled tasks were removed by user decision — exclude from the denominator
        # so completion_rate reflects only executable work, not cancelled tasks.
        cancelled = sum(1 for t in children if t.status == TASK_STATUS_CANCELLED)
        executable_total = total - cancelled
        stats.append(
            {
                "task_id": hl_task.id,
                "title": hl_task.title,
                "atomic_tasks_count": total,
                "completed_count": completed,
                "cancelled_count": cancelled,
                "reatomize_events": reatomized,
                # completion_rate denominator excludes cancelled tasks (user decisions,
                # not execution failures) to avoid artificially deflating the metric.
                "completion_rate": (
                    round(completed / executable_total, 2) if executable_total > 0 else None
                ),
            }
        )

    total_atomic = sum(s["atomic_tasks_count"] for s in stats)
    total_cancelled = sum(s["cancelled_count"] for s in stats)
    total_completed = sum(s["completed_count"] for s in stats)
    total_executable = total_atomic - total_cancelled

    return {
        "high_level_tasks": stats,
        "total_atomic_tasks": total_atomic,
        "total_cancelled_tasks": total_cancelled,
        # overall_completion_rate excludes cancelled tasks from the denominator.
        "overall_completion_rate": (
            round(total_completed / total_executable, 2) if total_executable > 0 else None
        ),
    }


def _build_user_prompt(
    *,
    project_id: int,
    project_name: str,
    project_description: str,
    system_prompt: str,
    trace_entries: list[dict],
    task_stats: dict,
) -> str:
    prompt_loader.validate_builder_inputs(
        "planner_evaluator",
        "main",
        {
            "project_id": project_id,
            "project_name": project_name,
            "project_description": project_description,
            "system_prompt": system_prompt,
            "planning_trace_entries": trace_entries,
            "tasks_stats": task_stats,
        },
    )
    return f"""
Evaluate the planner agent's performance for this project.

Project:
- project_id: {project_id}
- name: {project_name}
- description: {project_description}

System prompt that was active during planning:
---
{system_prompt}
---

Planning trace entries (all calls with agent=planner for this project):
{json.dumps(trace_entries, ensure_ascii=False, indent=2)}

Task outcome statistics:
{json.dumps(task_stats, ensure_ascii=False, indent=2)}

Instructions:
- Evaluate based on the system prompt guidance, the trace entries (inputs and reasoning), and the task outcome statistics.
- Look for evidence of scope gaps, granularity problems, vague reasoning, or executor bias.
- High reatomize_events for a high-level task may indicate that the planner produced tasks that were hard to decompose atomically.
- Low completion rates for children of a specific high-level task may indicate scope or clarity problems in that task.
- Be specific and evidence-based. Produce a verdict, findings prose, concrete issues list, and suggestions.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "planner_evaluator",
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


class PlannerEvaluator:
    """Evaluates the planner agent for a project and returns an AgentEvaluationOutput."""

    AGENT_NAME = "planner"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        trace_entries = _load_planner_trace_entries(project_id)

        if not trace_entries:
            return EvaluatorOutput(result=None)

        task_stats = _compute_task_stats(db, project_id)

        system_prompt = resolve_system_prompt(
            "planner",
            system_version=system_version,
        )

        user_prompt = _build_user_prompt(
            project_id=project_id,
            project_name=project_name,
            project_description=project_description,
            system_prompt=system_prompt,
            trace_entries=trace_entries,
            task_stats=task_stats,
        )

        provider = get_llm_provider()
        raw = provider.generate_structured(
            system_prompt=PLANNER_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="planner_evaluator_output",
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
                system_prompt=PLANNER_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="planner_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
