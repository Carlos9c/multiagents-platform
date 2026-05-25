"""QueryTool — wraps answer_project_query()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.project_query_agent import ProjectQueryError, answer_project_query


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

        try:
            answer = answer_project_query(
                db,
                project_id=project_id,
                project_name=project_name,
                project_goal=project_goal,
                user_question=question,
                conversation_phase=conversation.phase,
            )
            return ToolResult(
                tool_name=ToolName.QUERY,
                data={"answer": answer},
            )
        except ProjectQueryError as exc:
            return ToolResult(
                tool_name=ToolName.QUERY,
                data={"answer": None, "error": str(exc)},
            )
