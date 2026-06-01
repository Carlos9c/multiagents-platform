"""Builds a lightweight ProjectSnapshot from DB for Aria's prompt.

No LLM calls here — pure DB reads. The snapshot is serialised into the
user-prompt so Aria always has an up-to-date view of the project without
needing to call QueryAgent just to orient itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.conversation import CONVERSATION_PHASE_REVIEWING, Conversation
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Task,
)
from app.services.conversation.aria.contracts import (
    ReviewContext,
    review_context_from_json,
)


@dataclass
class TaskCounts:
    completed: int = 0
    failed: int = 0
    pending: int = 0
    running: int = 0
    blocked: int = 0  # awaiting_review or partial


@dataclass
class ProjectSnapshot:
    project_id: int
    project_name: str
    project_description: str | None
    # Phase is a hint for Aria — not a hard routing constraint.
    phase: str
    requirements_ready: bool
    proposed_plan: str | None
    review_context: ReviewContext | None
    task_counts: TaskCounts
    requirements_draft: str | None
    # Present only when phase == "reviewing".
    # "gathering_clarification" — no plan yet; collecting info via review_agent.
    # "awaiting_confirmation"   — plan proposed (proposed_plan set); waiting for user yes/no.
    review_subphase: str | None = None

    def format_for_prompt(self) -> str:
        """Serialise the snapshot into a compact text block for the LLM prompt."""
        lines: list[str] = [
            f"Proyecto: {self.project_name}",
            f"Fase actual: {self.phase}",
        ]

        if self.review_subphase:
            lines.append(f"Sub-fase de revisión: {self.review_subphase}")

        if self.project_description:
            desc = self.project_description[:300]
            if len(self.project_description) > 300:
                desc += "…"
            lines.append(f"Descripción: {desc}")

        counts = self.task_counts
        if any([counts.completed, counts.failed, counts.pending, counts.running, counts.blocked]):
            lines.append(
                f"Tareas: {counts.completed} completadas / {counts.failed} fallidas / "
                f"{counts.pending} pendientes / {counts.running} en curso / "
                f"{counts.blocked} bloqueadas"
            )

        if self.requirements_ready:
            lines.append("Estado: requisitos completos — pendiente confirmación de inicio")

        if self.proposed_plan:
            plan_preview = self.proposed_plan[:200]
            if len(self.proposed_plan) > 200:
                plan_preview += "…"
            lines.append(f"Plan propuesto (pendiente de confirmación): {plan_preview}")

        if self.review_context is not None:
            ctx = self.review_context
            if ctx.kind == "task":
                lines.append(
                    f"Review activo: tarea «{ctx.task_title}» bloqueada. "
                    f"Notas: {ctx.validation_notes or 'sin notas'}"
                )
            else:
                lines.append(
                    f"Review activo: error de proyecto ({ctx.failure_type}). "
                    f"Razón: {ctx.failure_reason[:200]}"
                )

        return "\n".join(lines)


def build_snapshot(
    db: Session,
    project_id: int,
    conversation: Conversation,
) -> ProjectSnapshot:
    """Build a ProjectSnapshot from the current DB state. No LLM calls."""
    from app.models.project import Project  # avoid circular import at module level

    project = db.get(Project, project_id)
    project_name = (project.name if project else None) or "Proyecto"
    project_description = project.description if project else None

    # Task counts (atomic tasks only)
    atomic_tasks: list[Task] = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .all()
    )

    counts = TaskCounts()
    for t in atomic_tasks:
        if t.status == TASK_STATUS_COMPLETED:
            counts.completed += 1
        elif t.status == TASK_STATUS_FAILED:
            counts.failed += 1
        elif t.status == TASK_STATUS_PENDING:
            counts.pending += 1
        elif t.status == TASK_STATUS_RUNNING:
            counts.running += 1
        elif t.status in (TASK_STATUS_AWAITING_REVIEW, TASK_STATUS_PARTIAL):
            counts.blocked += 1

    # Deserialise review context if present
    review_ctx: ReviewContext | None = None
    if conversation.review_context:
        try:
            review_ctx = review_context_from_json(conversation.review_context)
        except Exception:
            review_ctx = None

    # Compute review sub-phase: distinguishes between "still gathering info"
    # and "plan presented, waiting for user's yes/no".
    review_subphase: str | None = None
    if conversation.phase == CONVERSATION_PHASE_REVIEWING:
        review_subphase = (
            "awaiting_confirmation" if conversation.proposed_plan else "gathering_clarification"
        )

    return ProjectSnapshot(
        project_id=project_id,
        project_name=project_name,
        project_description=project_description,
        phase=conversation.phase,
        requirements_ready=conversation.requirements_ready,
        proposed_plan=conversation.proposed_plan,
        review_context=review_ctx,
        task_counts=counts,
        requirements_draft=conversation.requirements_draft,
        review_subphase=review_subphase,
    )
