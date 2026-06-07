"""ResumptionTool — wraps resume_after_review()."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.services.conversation.aria.contracts import (
    ProjectReviewContext,
    TaskReviewContext,
    ToolName,
    ToolResult,
    review_context_from_json,
)
from app.services.conversation.resumption_service import ResumptionError, resume_after_review

_ARTIFACT_TYPE = "review_episode"
_ARTIFACT_CREATED_BY = "resumption_tool"

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

        # Capture review episode state BEFORE resumption mutates conversation fields
        review_attempts = self._conversation_review_attempts(db, project_id)
        proposed_plan = clarification  # clarification == conversation.proposed_plan at this point

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

        outcome = "disruptive_restart" if result.scope == "disruptive" else "confirmed"
        self._save_episode_artifact(
            db=db,
            project_id=project_id,
            is_project_level=False,
            blocked_task_id=review_ctx.task_id,
            blocked_task_title=review_ctx.task_title,
            review_attempts=review_attempts,
            proposed_plan=proposed_plan,
            outcome=outcome,
            impact_scope=result.scope,
            tasks_modified_count=result.tasks_modified,
            tasks_added_count=result.tasks_added,
            tasks_eliminated_count=result.tasks_eliminated,
            tasks_superseded_count=result.tasks_superseded,
            impact_reasoning=result.reasoning,
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

        review_attempts = self._conversation_review_attempts(db, project_id)

        existing = project.description or ""
        project.description = existing + _PROJECT_CLARIFICATION_HEADER + clarification.strip()
        db.add(project)
        db.flush()

        self._save_episode_artifact(
            db=db,
            project_id=project_id,
            is_project_level=True,
            blocked_task_id=None,
            blocked_task_title=None,
            review_attempts=review_attempts,
            proposed_plan=clarification,
            outcome="confirmed",
            impact_scope=None,
            tasks_modified_count=0,
            tasks_added_count=0,
            tasks_eliminated_count=0,
            tasks_superseded_count=0,
            impact_reasoning=None,
        )

        return ToolResult(
            tool_name=ToolName.RESUMPTION,
            data={
                "scope": "project_level",
                "message": "Configuración del proyecto actualizada con las indicaciones del usuario.",
                "project_id": project_id,
            },
            side_effects_summary="Descripción del proyecto actualizada con la indicación post-revisión.",
        )

    # ------------------------------------------------------------------
    # Supervisor instrumentation
    # ------------------------------------------------------------------

    @staticmethod
    def _conversation_review_attempts(db: Session, project_id: int) -> int:
        from app.models.conversation import CONVERSATION_STATUS_ACTIVE, Conversation

        conv = (
            db.query(Conversation)
            .filter(
                Conversation.project_id == project_id,
                Conversation.status == CONVERSATION_STATUS_ACTIVE,
            )
            .first()
        )
        return (conv.review_episode_attempts or 0) if conv else 0

    @staticmethod
    def _save_episode_artifact(
        db: Session,
        *,
        project_id: int,
        is_project_level: bool,
        blocked_task_id: int | None,
        blocked_task_title: str | None,
        review_attempts: int,
        proposed_plan: str | None,
        outcome: str,
        impact_scope: str | None,
        tasks_modified_count: int,
        tasks_added_count: int,
        tasks_eliminated_count: int,
        tasks_superseded_count: int,
        impact_reasoning: str | None,
    ) -> None:
        """Persist a review_episode artifact for the Supervisor."""
        try:
            episode_index = (
                db.query(Artifact)
                .filter(
                    Artifact.project_id == project_id,
                    Artifact.artifact_type == _ARTIFACT_TYPE,
                )
                .count()
            )
            payload = {
                "episode_index": episode_index,
                "is_project_level": is_project_level,
                "blocked_task_id": blocked_task_id,
                "blocked_task_title": blocked_task_title,
                "review_attempts": review_attempts,
                "proposed_plan": proposed_plan,
                "outcome": outcome,
                "impact_scope": impact_scope,
                "tasks_modified_count": tasks_modified_count,
                "tasks_added_count": tasks_added_count,
                "tasks_eliminated_count": tasks_eliminated_count,
                "tasks_superseded_count": tasks_superseded_count,
                "impact_reasoning": impact_reasoning,
            }
            artifact = Artifact(
                project_id=project_id,
                task_id=blocked_task_id,
                artifact_type=_ARTIFACT_TYPE,
                content=json.dumps(payload),
                created_by=_ARTIFACT_CREATED_BY,
            )
            db.add(artifact)
            db.flush()
        except Exception:
            pass  # Supervisor instrumentation must never break the agent
