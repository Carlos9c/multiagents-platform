"""ReviewTool — wraps evaluate_review()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationMessage
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.services.conversation.aria.contracts import (
    ProjectReviewContext,
    TaskReviewContext,
    ToolName,
    ToolResult,
    review_context_from_json,
)
from app.services.conversation.requirements_evaluator import ConversationTurn
from app.services.conversation.review_evaluator import (
    BlockedTaskContext,
    ReviewEvaluatorInput,
    evaluate_review,
)


class ReviewTool:
    """Evaluates user clarification during a review episode.

    Fetches the review context, episode history, and task summary from DB.
    Works uniformly for both task-level and project-level review contexts.
    """

    @property
    def name(self) -> ToolName:
        return ToolName.REVIEW

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult:
        from app.models.project import Project

        project = db.get(Project, project_id)
        project_goal = (
            (project.description if project else None)
            or (project.name if project else None)
            or "Proyecto"
        )

        # Parse review context
        review_ctx = None
        if conversation.review_context:
            try:
                review_ctx = review_context_from_json(conversation.review_context)
            except Exception:
                pass

        is_project_level = review_ctx is None or review_ctx.kind == "project"

        # Build BlockedTaskContext
        if is_project_level:
            failure_reason = (
                review_ctx.failure_reason
                if isinstance(review_ctx, ProjectReviewContext)
                else "Error desconocido en el flujo de trabajo."
            )
            blocked_ctx = BlockedTaskContext(
                task_id=0,
                title="Error en el flujo de trabajo del proyecto",
                description=failure_reason,
                validation_notes=None,
            )
        else:
            assert isinstance(review_ctx, TaskReviewContext)
            blocked_ctx = BlockedTaskContext(
                task_id=review_ctx.task_id,
                title=review_ctx.task_title,
                description=review_ctx.task_description,
                validation_notes=review_ctx.validation_notes,
            )

        # Build episode history
        episode_history = self._load_episode_history(db, conversation, review_ctx)

        # Build task summary
        task_summary = self._build_task_summary(
            db,
            project_id,
            blocked_task_id=(
                review_ctx.task_id if isinstance(review_ctx, TaskReviewContext) else None
            ),
        )

        result = evaluate_review(
            ReviewEvaluatorInput(
                project_goal=project_goal,
                blocked_task=blocked_ctx,
                episode_history=episode_history,
                project_task_summary=task_summary,
                is_project_level_error=is_project_level,
            )
        )

        return ToolResult(
            tool_name=ToolName.REVIEW,
            data={
                "status": result.status,
                "next_question": result.next_question,
                "clarification_summary": result.clarification_summary,
                "action_summary": result.action_summary,
                "reasoning": result.reasoning,
                "is_project_level": is_project_level,
            },
        )

    def _load_episode_history(
        self,
        db: Session,
        conversation: Conversation,
        review_ctx: TaskReviewContext | ProjectReviewContext | None,
    ) -> list[ConversationTurn]:
        """Load messages relevant to the current review episode."""
        # For project-level reviews or when context is missing, return full history
        if review_ctx is None or review_ctx.kind == "project":
            messages = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == conversation.id)
                .order_by(ConversationMessage.id.asc())
                .all()
            )
        else:
            # Task-level: messages since the conversation entered reviewing phase.
            # We use the last reviewing-phase entry point by finding the most recent
            # system message that introduced the review, then taking all messages after it.
            all_messages = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == conversation.id)
                .order_by(ConversationMessage.id.asc())
                .all()
            )
            # Find last system message (review opening) and include from there
            last_system_idx = -1
            for i, m in enumerate(all_messages):
                if m.role == "system":
                    last_system_idx = i
            messages = all_messages[last_system_idx:] if last_system_idx >= 0 else all_messages

        return [
            ConversationTurn(role=m.role, content=m.content)  # type: ignore[arg-type]
            for m in messages
            if m.role in ("user", "assistant", "system")
        ]

    def _build_task_summary(
        self,
        db: Session,
        project_id: int,
        *,
        blocked_task_id: int | None,
    ) -> str | None:
        tasks = (
            db.query(Task)
            .filter(
                Task.project_id == project_id,
                Task.planning_level == PLANNING_LEVEL_ATOMIC,
            )
            .all()
        )
        if not tasks:
            return None

        completed = [
            t for t in tasks if t.status == TASK_STATUS_COMPLETED and t.id != blocked_task_id
        ]
        failed = [t for t in tasks if t.status == TASK_STATUS_FAILED and t.id != blocked_task_id]
        pending = [
            t for t in tasks if t.status not in TERMINAL_TASK_STATUSES and t.id != blocked_task_id
        ]

        parts: list[str] = []
        if completed:
            parts.append(
                f"Completed ({len(completed)}): " + ", ".join(f"«{t.title}»" for t in completed)
            )
        if failed:
            parts.append(f"Failed ({len(failed)}): " + ", ".join(f"«{t.title}»" for t in failed))
        if pending:
            parts.append(f"Pending ({len(pending)}): " + ", ".join(f"«{t.title}»" for t in pending))

        return "\n".join(parts) if parts else None
