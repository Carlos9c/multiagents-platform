"""Contracts for the Aria conversation orchestrator.

Aria is the conversational face of the system. It receives inputs from two sources:
- User messages (via WebSocket)
- System events (from the workflow layer: review required, error, completion, etc.)

On each turn Aria runs a lightweight loop, calling tools as needed and then
synthesising a final response. All public types for that loop live here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ── Tool names ────────────────────────────────────────────────────────────────


class ToolName(str, Enum):
    REQUIREMENTS = "requirements_agent"
    QUERY = "query_agent"
    REVIEW = "review_agent"
    CONFIRMATION = "confirmation_agent"
    START_PROJECT = "start_project"
    RESUMPTION = "resumption_agent"
    QA = "qa_tool"
    RESUME_PROJECT = "resume_project"


# ── Aria's per-step decision (LLM output schema) ──────────────────────────────


class AriaStep(BaseModel):
    """Structured output produced by Aria on each iteration of the loop.

    If action="call_tool" the orchestrator executes the named tool and loops.
    If action="respond" the orchestrator persists the response and returns.
    """

    action: Literal["call_tool", "respond"]
    tool_name: ToolName | None = None
    # Optional free-text guidance forwarded to the tool so it can focus its fetch/query.
    tool_hint: str | None = None
    # Final message to the user — only used when action="respond".
    response: str | None = None
    # Always present; used for debugging and traceability.
    reasoning: str


# ── Tool result ───────────────────────────────────────────────────────────────


class ToolResult(BaseModel):
    tool_name: ToolName
    # Structured data returned by the tool — fed back into Aria's next iteration.
    data: dict[str, Any]
    # Human-readable summary of any DB side effects (task mutations, phase changes, etc.)
    side_effects_summary: str | None = None


# ── Review context (discriminated union) ─────────────────────────────────────


class TaskReviewContext(BaseModel):
    """Context when a specific task is blocked and needs user clarification."""

    kind: Literal["task"] = "task"
    task_id: int
    task_title: str
    task_description: str | None = None
    validation_notes: str | None = None


class ProjectReviewContext(BaseModel):
    """Context when a project-level failure (no task anchor) requires user input."""

    kind: Literal["project"] = "project"
    failure_type: Literal["bootstrap", "plan_empty", "iteration_limit", "unknown"]
    failure_reason: str


# Discriminated union — serialise/deserialise via review_context_from_json / to_json helpers.
ReviewContext = Annotated[
    TaskReviewContext | ProjectReviewContext,
    Field(discriminator="kind"),
]


def review_context_to_json(ctx: TaskReviewContext | ProjectReviewContext) -> str:
    return ctx.model_dump_json()


def review_context_from_json(raw: str) -> TaskReviewContext | ProjectReviewContext:
    data = json.loads(raw)
    if data.get("kind") == "task":
        return TaskReviewContext.model_validate(data)
    return ProjectReviewContext.model_validate(data)


def classify_workflow_failure(
    failure_reason: str,
) -> Literal["bootstrap", "plan_empty", "iteration_limit", "unknown"]:
    lower = failure_reason.lower()
    if any(kw in lower for kw in ("environment", "docker", "bootstrap", "entorno", "imagen")):
        return "bootstrap"
    if any(
        kw in lower for kw in ("empty", "vacío", "no batches", "sin tareas", "empty_execution_plan")
    ):
        return "plan_empty"
    if any(
        kw in lower for kw in ("iteration", "iteraci", "limit", "límite", "exhausted", "agotado")
    ):
        return "iteration_limit"
    return "unknown"


# ── System events ─────────────────────────────────────────────────────────────


class SystemEventType(str, Enum):
    EXECUTION_STARTED = "execution_started"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    WORKFLOW_ERROR = "workflow_error"
    PROJECT_COMPLETED = "project_completed"
    CONFIRM_START = "confirm_start"
    QA_COMPLETED = "qa_completed"


class SystemEvent(BaseModel):
    event_type: SystemEventType
    data: dict[str, Any] = Field(default_factory=dict)


# ── Orchestrator input ────────────────────────────────────────────────────────


class AriaInput(BaseModel):
    """Unified input to the Aria orchestrator — from the user or from the system."""

    source: Literal["user", "system"]
    user_message: str | None = None
    system_event: SystemEvent | None = None


# ── Orchestrator response ─────────────────────────────────────────────────────


@dataclass
class AriaResponse:
    message: str
    phase: str
    conversation_id: int
    event: Literal[
        "question",
        "requirements_ready",
        "project_started",
        "execution_started",
        "review_started",
        "review_resolved",
        "review_confirmation_requested",
        "review_closed_abandoned",
        "project_query_answered",
        "project_completed",
        "qa_offer",
        "qa_running",
        "qa_completed_no_issues",
        "qa_completed_with_findings",
        "qa_remediation_started",
        "workflow_resumed",
        "fallback",
    ]
    project_id: int | None = None
    requirements_draft: str | None = None
    requirements_ready: bool = False


# ── Orchestrator error ────────────────────────────────────────────────────────


class AriaOrchestratorError(Exception):
    pass
