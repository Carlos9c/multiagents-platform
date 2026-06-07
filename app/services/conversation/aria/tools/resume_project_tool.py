"""ResumeProjectTool — conversational equivalent of POST /resume-workflow."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_PENDING, Task
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.project_workflow_service import clear_workflow_stop


class ResumeProjectTool:
    """Resumes a paused project workflow from within the conversation.

    Verifies pending tasks exist, clears the workflow stop signal, and returns
    a structured result. The orchestrator applies the phase transition and the
    WS router submits the background workflow job when it sees "workflow_resumed".
    """

    @property
    def name(self) -> ToolName:
        return ToolName.RESUME_PROJECT

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: object,
        hint: str | None = None,
    ) -> ToolResult:
        has_pending = (
            db.query(Task)
            .filter(Task.project_id == project_id, Task.status == TASK_STATUS_PENDING)
            .first()
        ) is not None

        if not has_pending:
            return ToolResult(
                tool_name=ToolName.RESUME_PROJECT,
                data={
                    "status": "no_pending_tasks",
                    "message": "No hay tareas pendientes para reanudar.",
                },
            )

        clear_workflow_stop(project_id)

        return ToolResult(
            tool_name=ToolName.RESUME_PROJECT,
            data={
                "status": "resumed",
                "message": "El proyecto ha sido reanudado.",
                "project_id": project_id,
            },
            side_effects_summary="Señal de pausa eliminada; la ejecución continuará.",
        )
