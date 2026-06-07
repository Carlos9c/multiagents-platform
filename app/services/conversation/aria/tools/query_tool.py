"""QueryTool — wraps answer_project_query()."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.execution_run import ExecutionRun
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    Task,
)
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.language_utils import detect_language_from_text
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

        # Fetch error summaries for failed / partial tasks so QueryAgent can explain failures
        error_summaries = self._get_error_summaries(db, project_id)

        # Detect language from the question text
        detected_language = detect_language_from_text(question)

        try:
            answer = answer_project_query(
                db,
                project_id=project_id,
                project_name=project_name,
                project_goal=project_goal,
                user_question=question,
                conversation_phase=conversation.phase,
                error_summaries=error_summaries,
                language=detected_language,
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
    def _get_error_summaries(db: Session, project_id: int) -> dict[int, str]:
        """Return a {task_id: error_summary} map for failed/partial atomic tasks.

        Uses the latest ExecutionRun per task, combining validation_notes + blockers_found
        + error_message (same pattern as resumption_service._get_validation_notes).
        Only queries failed/partial tasks to keep the token budget manageable.
        """
        failed_task_ids: list[int] = [
            row[0]
            for row in db.query(Task.id)
            .filter(
                Task.project_id == project_id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
                Task.status.in_([TASK_STATUS_FAILED, TASK_STATUS_PARTIAL]),
            )
            .all()
        ]
        result: dict[int, str] = {}
        for task_id in failed_task_ids:
            last_run = (
                db.query(ExecutionRun)
                .filter(ExecutionRun.task_id == task_id)
                .order_by(ExecutionRun.id.desc())
                .first()
            )
            if last_run is None:
                continue
            parts = [
                last_run.validation_notes,
                last_run.blockers_found,
                last_run.error_message,
            ]
            combined = " | ".join(p for p in parts if p)
            if combined:
                result[task_id] = combined
        return result

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
