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

logger = logging.getLogger(__name__)


class ProjectQueryLLMOutput(BaseModel):
    message: str


class ProjectQueryError(Exception):
    pass


_SYSTEM_PROMPT = """
You are Aria, a project assistant for an autonomous software development system.
You are warm, precise, and professionally cheerful.

The user has a question or comment about the project while it is running (or has
finished). Answer naturally using the project state information provided.

You can answer questions such as:
- "What has been done so far?"
- "What is left to do?"
- "What failed and why?"
- "Can you give me a summary of progress?"
- "How many tasks are left?"
- "What is currently running?"

Rules:
- Answer in the same language the user wrote in.
- Be concise but informative. Reference specific task titles when relevant.
- Be honest about uncertainty — if you don't have details about why something
  failed, say so.
- Do NOT claim you can take action during execution. If the user asks you to do
  something, politely explain that the automated process is running and you cannot
  intervene mid-execution. Suggest using the Pause button if they need to make
  changes, or waiting until the project finishes.
- During PAUSED phase: explain that execution is paused and can be resumed.
- During COMPLETED phase: give a final summary and congratulate if all succeeded.
- Keep responses conversational, warm, and to the point.
""".strip()


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
