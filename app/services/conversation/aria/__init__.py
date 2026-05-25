"""Aria conversation orchestrator package."""

from app.services.conversation.aria.contracts import (
    AriaInput,
    AriaOrchestratorError,
    AriaResponse,
    AriaStep,
    ProjectReviewContext,
    SystemEvent,
    SystemEventType,
    TaskReviewContext,
    ToolName,
    ToolResult,
)
from app.services.conversation.aria.orchestrator import (
    check_project_completed,
    get_or_create_conversation,
    process,
    process_with_pre_transitions,
)

__all__ = [
    # Contracts
    "AriaInput",
    "AriaOrchestratorError",
    "AriaResponse",
    "AriaStep",
    "ProjectReviewContext",
    "SystemEvent",
    "SystemEventType",
    "TaskReviewContext",
    "ToolName",
    "ToolResult",
    # Orchestrator API
    "get_or_create_conversation",
    "process",
    "process_with_pre_transitions",
    "check_project_completed",
]
