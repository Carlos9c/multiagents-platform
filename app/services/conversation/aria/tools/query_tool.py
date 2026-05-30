"""QueryTool — wraps answer_project_query()."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.project_query_agent import ProjectQueryError, answer_project_query

_ARTIFACT_TYPE = "project_query"
_ARTIFACT_CREATED_BY = "query_tool"


class QueryTool:
    """Answers natural language questions about the project's current state.

    Available in any phase — Aria uses this both for general Q&A and for
    providing context during review episodes.
    """

    @property
    def name(self) -> ToolName:
        return ToolName.QUERY

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult:
        from app.models.project import Project

        project = db.get(Project, project_id)
        project_name = (project.name if project else None) or "Proyecto"
        project_goal = (project.description if project else None) or project_name

        # hint carries the user's question as Aria understood it
        question = hint or "¿Cuál es el estado actual del proyecto?"

        task_counts = self._count_tasks(db, project_id)

        try:
            answer = answer_project_query(
                db,
                project_id=project_id,
                project_name=project_name,
                project_goal=project_goal,
                user_question=question,
                conversation_phase=conversation.phase,
            )
            self._save_artifact(db, project_id, question, answer, conversation.phase, task_counts)
            return ToolResult(
                tool_name=ToolName.QUERY,
                data={"answer": answer},
            )
        except ProjectQueryError as exc:
            return ToolResult(
                tool_name=ToolName.QUERY,
                data={"answer": None, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Supervisor instrumentation
    # ------------------------------------------------------------------

    @staticmethod
    def _count_tasks(db: Session, project_id: int) -> dict:
        tasks = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
            )
            .all()
        )
        completed = sum(1 for t in tasks if t.status == TASK_STATUS_COMPLETED)
        pending = sum(1 for t in tasks if t.status == TASK_STATUS_PENDING)
        failed = sum(1 for t in tasks if t.status == TASK_STATUS_FAILED)
        running = len(tasks) - completed - pending - failed
        return {"completed": completed, "pending": pending, "failed": failed, "running": running}

    @staticmethod
    def _save_artifact(
        db: Session,
        project_id: int,
        user_question: str,
        aria_response: str,
        conversation_phase: str,
        task_counts: dict,
    ) -> None:
        """Persist a project_query artifact for the Supervisor."""
        try:
            query_index = (
                db.query(Artifact)
                .filter(
                    Artifact.project_id == project_id,
                    Artifact.artifact_type == _ARTIFACT_TYPE,
                )
                .count()
            )
            payload = {
                "query_index": query_index,
                "conversation_phase": conversation_phase,
                "user_question": user_question,
                "aria_response": aria_response,
                "task_counts": task_counts,
            }
            artifact = Artifact(
                project_id=project_id,
                task_id=None,
                artifact_type=_ARTIFACT_TYPE,
                content=json.dumps(payload),
                created_by=_ARTIFACT_CREATED_BY,
            )
            db.add(artifact)
            db.flush()
        except Exception:
            pass  # Supervisor instrumentation must never break the agent
