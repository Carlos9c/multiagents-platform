"""ResumptionTool — wraps resume_after_review()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.services.conversation.aria.contracts import (
    ProjectReviewContext,
    TaskReviewContext,
    ToolName,
    ToolResult,
    review_context_from_json,
)
from app.services.conversation.resumption_service import ResumptionError, resume_after_review

_PROJECT_CLARIFICATION_HEADER = "\n\n--- INDICACIÓN POST-REVISIÓN ---\n"


class ResumptionTool:
    """Applies the confirmed clarification to the task graph and resumes execution.

    For task-level reviews: calls resume_after_review() which handles narrow /
    moderate / disruptive scope mutations.

    For project-level reviews: embeds the clarification in the project description
    and re-queues the workflow (no task to modify).

    On success the orchestrator clears proposed_plan, review_context, and
    transitions phase back to 'executing'.
    """

    @property
    def name(self) -> ToolName:
        return ToolName.RESUMPTION

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult:
        clarification = conversation.proposed_plan or hint or ""

        review_ctx = None
        if conversation.review_context:
            try:
                review_ctx = review_context_from_json(conversation.review_context)
            except Exception:
                pass

        if isinstance(review_ctx, TaskReviewContext):
            return self._resume_task_level(db, project_id, review_ctx, clarification)

        if isinstance(review_ctx, ProjectReviewContext):
            return self._resume_project_level(db, project_id, clarification)

        # No review context — nothing to resume
        return ToolResult(
            tool_name=ToolName.RESUMPTION,
            data={"error": "No review context found — nothing to resume."},
        )

    def _resume_task_level(
        self,
        db: Session,
        project_id: int,
        review_ctx: TaskReviewContext,
        clarification: str,
    ) -> ToolResult:
        from app.models.project import Project

        project = db.get(Project, project_id)
        try:
            result = resume_after_review(
                db,
                project_id=project_id,
                blocked_task_id=review_ctx.task_id,
                user_clarification=clarification,
                updated_project_goal=project.description if project else None,
            )
        except ResumptionError as exc:
            return ToolResult(
                tool_name=ToolName.RESUMPTION,
                data={"error": str(exc)},
            )

        return ToolResult(
            tool_name=ToolName.RESUMPTION,
            data={
                "scope": result.scope,
                "message": result.message,
                "tasks_modified": result.tasks_modified,
                "tasks_added": result.tasks_added,
                "project_id": project_id,
            },
            side_effects_summary=(
                f"Scope {result.scope}: {result.tasks_modified} tarea(s) modificadas, "
                f"{result.tasks_added} añadidas."
            ),
        )

    def _resume_project_level(
        self,
        db: Session,
        project_id: int,
        clarification: str,
    ) -> ToolResult:
        from app.models.project import Project

        project = db.get(Project, project_id)
        if project is None:
            return ToolResult(
                tool_name=ToolName.RESUMPTION,
                data={"error": f"Project {project_id} not found"},
            )

        existing = project.description or ""
        project.description = existing + _PROJECT_CLARIFICATION_HEADER + clarification.strip()
        db.add(project)
        db.flush()

        return ToolResult(
            tool_name=ToolName.RESUMPTION,
            data={
                "scope": "project_level",
                "message": "Configuración del proyecto actualizada con las indicaciones del usuario.",
                "project_id": project_id,
            },
            side_effects_summary="Descripción del proyecto actualizada con la indicación post-revisión.",
        )
