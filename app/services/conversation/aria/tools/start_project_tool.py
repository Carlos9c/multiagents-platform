"""StartProjectTool — wraps ProjectStartService.start()."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.schemas.project_start import ProjectStartRequest
from app.services.conversation.aria.contracts import ToolName, ToolResult
from app.services.project_start_service import ProjectStartService


class StartProjectTool:
    """Kicks off project planning: generates the atomic task plan.

    Uses the requirements draft (or project description) as the project goal.
    On success the orchestrator transitions conversation.phase to 'executing'.
    """

    @property
    def name(self) -> ToolName:
        return ToolName.START_PROJECT

    def execute(
        self,
        db: Session,
        project_id: int,
        conversation: Conversation,
        hint: str | None = None,
    ) -> ToolResult:
        from app.models.project import Project

        project = db.get(Project, project_id)
        if project is None:
            return ToolResult(
                tool_name=ToolName.START_PROJECT,
                data={"error": f"Project {project_id} not found"},
            )

        # Use requirements_draft as canonical description if available
        effective_description = (
            conversation.requirements_draft or project.description or project.name or ""
        )
        project.description = effective_description
        db.add(project)

        start_service = ProjectStartService()
        request = ProjectStartRequest(
            project_id=project.id,
            name=project.name,
            use_existing_source=True,
        )
        response = start_service.start(db, request)

        return ToolResult(
            tool_name=ToolName.START_PROJECT,
            data={
                "tasks_created": response.tasks_created,
                "project_id": project_id,
            },
            side_effects_summary=f"Plan creado con {response.tasks_created} tarea(s).",
        )
