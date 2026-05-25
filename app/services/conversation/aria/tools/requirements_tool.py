"""RequirementsTool — wraps evaluate_requirements()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationMessage
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.requirements_evaluator import (
    ConversationTurn,
    RequirementsEvaluatorInput,
    evaluate_requirements,
)


class RequirementsTool:
    """Evaluates whether the gathered requirements are sufficient to start the project.

    Fetches full conversation history and current requirements draft from DB.
    """

    @property
    def name(self) -> ToolName:
        return ToolName.REQUIREMENTS

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

        messages = (
            db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.id.asc())
            .all()
        )
        history = [
            ConversationTurn(role=m.role, content=m.content)  # type: ignore[arg-type]
            for m in messages
            if m.role in ("user", "assistant")
        ]

        result = evaluate_requirements(
            RequirementsEvaluatorInput(
                project_name=project_name,
                history=history,
                current_draft=conversation.requirements_draft,
            )
        )

        return ToolResult(
            tool_name=ToolName.REQUIREMENTS,
            data={
                "status": result.status,
                "next_question": result.next_question,
                "updated_draft": result.updated_draft,
                "reasoning": result.reasoning,
            },
        )
