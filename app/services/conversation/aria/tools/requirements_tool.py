"""RequirementsTool — wraps evaluate_requirements()."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.conversation import Conversation, ConversationMessage
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.conversation.requirements_evaluator import (
    ConversationTurn,
    RequirementsEvaluatorInput,
    RequirementsEvaluatorResult,
    evaluate_requirements,
)

_ARTIFACT_TYPE = "requirements_evaluation"
_ARTIFACT_CREATED_BY = "requirements_tool"


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

        self._save_artifact(db, project_id, result, len(history))

        return ToolResult(
            tool_name=ToolName.REQUIREMENTS,
            data={
                "status": result.status,
                "next_question": result.next_question,
                "updated_draft": result.updated_draft,
                "reasoning": result.reasoning,
            },
        )

    # ------------------------------------------------------------------
    # Supervisor instrumentation
    # ------------------------------------------------------------------

    @staticmethod
    def _save_artifact(
        db: Session,
        project_id: int,
        result: RequirementsEvaluatorResult,
        conversation_turn_count: int,
    ) -> None:
        """Persist a requirements_evaluation artifact for the Supervisor."""
        try:
            call_index = (
                db.query(Artifact)
                .filter(
                    Artifact.project_id == project_id,
                    Artifact.artifact_type == _ARTIFACT_TYPE,
                )
                .count()
            )
            draft_snapshot = (result.updated_draft or "")[:500] or None
            payload = {
                "call_index": call_index,
                "status": result.status,
                "next_question": result.next_question,
                "reasoning": result.reasoning,
                "draft_snapshot": draft_snapshot,
                "conversation_turn_count": conversation_turn_count,
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
