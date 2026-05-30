"""Project Query Agent.

Answers user questions about the project during EXECUTING, PAUSED, or COMPLETED
phases. Aria can respond to questions about progress, completed work, pending tasks,
and failures, without taking any action.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)


class ProjectQueryLLMOutput(BaseModel):
    message: str


class ProjectQueryError(Exception):
    pass


_SYSTEM_PROMPT = prompt_loader.get("project_query_agent")


def _render_task_list(tasks: list[dict], label: str) -> str:
    if not tasks:
        return f"{label}: none"
    lines = [f"{label} ({len(tasks)}):"]
    for t in tasks:
        lines.append(f"  - {t['title']}")
    return "\n".join(lines)


def _build_user_prompt(
    *,
    project_name: str,
    project_goal: str,
    user_question: str,
    conversation_phase: str,
    tasks_completed: list[dict],
    tasks_pending: list[dict],
    tasks_failed: list[dict],
    tasks_running: list[dict],
) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "project_query_agent",
        "main",
        {
            "project_name": project_name,
            "project_goal": project_goal,
            "user_question": user_question,
            "conversation_phase": conversation_phase,
            "completed_tasks": tasks_completed,
            "pending_tasks": tasks_pending,
            "failed_tasks": tasks_failed,
            "running_tasks": tasks_running,
        },
    )
    phase_note = {
        "executing": "The project is currently being executed automatically.",
        "paused": "Project execution is currently PAUSED by user request.",
        "completed": "The project has finished execution.",
        "awaiting_review": "A task is currently awaiting your review.",
    }.get(conversation_phase, f"Phase: {conversation_phase}")

    return f"""
PROJECT: {project_name}
GOAL: {project_goal}
STATUS: {phase_note}

{_render_task_list(tasks_completed, "COMPLETED TASKS")}
{_render_task_list(tasks_running, "CURRENTLY RUNNING")}
{_render_task_list(tasks_pending, "PENDING TASKS")}
{_render_task_list(tasks_failed, "FAILED TASKS")}

USER MESSAGE:
{user_question}
""".strip()


def answer_project_query(
    db: Session,
    *,
    project_id: int,
    project_name: str,
    project_goal: str,
    user_question: str,
    conversation_phase: str,
) -> str:
    """Query project state and return a natural language answer from Aria."""
    tasks = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .order_by(Task.sequence_order.asc().nullslast())
        .all()
    )

    completed: list[dict] = []
    pending: list[dict] = []
    failed: list[dict] = []
    running: list[dict] = []

    for t in tasks:
        entry = {"id": t.id, "title": t.title}
        if t.status == TASK_STATUS_COMPLETED:
            completed.append(entry)
        elif t.status == TASK_STATUS_PENDING:
            pending.append(entry)
        elif t.status == TASK_STATUS_FAILED:
            failed.append(entry)
        else:
            running.append(entry)  # running, awaiting_review, partial, etc.

    provider = get_llm_provider()
    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=_build_user_prompt(
            project_name=project_name,
            project_goal=project_goal,
            user_question=user_question,
            conversation_phase=conversation_phase,
            tasks_completed=completed,
            tasks_pending=pending,
            tasks_failed=failed,
            tasks_running=running,
        ),
        schema_name="project_query_output",
        json_schema=ProjectQueryLLMOutput.model_json_schema(),
    )

    try:
        output = ProjectQueryLLMOutput.model_validate(raw)
    except ValidationError as exc:
        raise ProjectQueryError(
            f"ProjectQueryAgent returned invalid structured output: {exc}"
        ) from exc

    logger.info(
        "project_query_answered project_id=%s phase=%s",
        project_id,
        conversation_phase,
    )

    return output.message
