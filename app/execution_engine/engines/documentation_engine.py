from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.execution_engine.agent_runtime import StructuredLLMRuntime
from app.execution_engine.contracts import ExecutionRequest, ExecutionResult
from app.execution_engine.execution_plan import ExecutionStep
from app.execution_engine.resolution_state import ResolutionState
from app.execution_engine.subagents.base import SubagentRejectedStepError
from app.execution_engine.subagents.context_selection_agent import ContextSelectionAgent
from app.execution_engine.subagents.document_writer_agent import DocumentWriterAgent

logger = logging.getLogger(__name__)

_CONTEXT_STEP = ExecutionStep(
    id="doc_context_selection",
    subagent_name="context_selection_agent",
    title="Select historical context",
    instructions=(
        "Select relevant completed historical tasks and runs that provide necessary "
        "context for this documentation or design task."
    ),
)


class DocumentationEngine:
    """
    Linear execution engine for documentation and design tasks.

    Runs a fixed two-step sequence: context selection followed by document writing.
    No orchestrator loop, no Docker environment, no command execution.
    """

    backend_name = "documentation"

    def execute(
        self, db: Session, request: ExecutionRequest
    ) -> tuple[ExecutionResult, ExecutionRequest]:
        context_runtime = StructuredLLMRuntime(
            model=settings.execution_engine_model,
            provider=settings.execution_engine_provider,
        )
        doc_writer_runtime = StructuredLLMRuntime(
            model=settings.documentation_agent_model,
            provider=settings.documentation_agent_provider,
        )

        context_agent = ContextSelectionAgent(runtime=context_runtime)
        doc_writer = DocumentWriterAgent(runtime=doc_writer_runtime)

        state = ResolutionState(execution_request=request)

        # Step 1: context selection (non-fatal — proceed without history if it fails)
        try:
            state = context_agent.execute_step(
                db=db,
                request=request,
                step=_CONTEXT_STEP,
                state=state,
            )
            request = state.execution_request
        except SubagentRejectedStepError as exc:
            logger.warning(
                "documentation_engine_context_selection_failed task_id=%s error=%s proceeding=true",
                request.task_id,
                str(exc),
            )
            state.add_note(f"Context selection skipped: {str(exc)}")

        # Step 2: document writing
        doc_step = ExecutionStep(
            id="doc_writing",
            subagent_name="document_writer_agent",
            title="Write documentation artifacts",
            instructions=(
                f"Write all required documentation and design artifacts for: {request.task_title}"
            ),
        )

        try:
            state = doc_writer.execute_step(
                db=db,
                request=request,
                step=doc_step,
                state=state,
            )
        except SubagentRejectedStepError as exc:
            logger.error(
                "documentation_engine_writer_failed task_id=%s error=%s",
                request.task_id,
                str(exc),
            )
            return ExecutionResult(
                task_id=request.task_id,
                decision="failed",
                summary=f"Document writer failed: {str(exc)}",
                execution_agent_sequence=["context_selection_agent", "document_writer_agent"],
                evidence=state.evidence,
            ), request

        files_written = state.evidence.changed_files
        file_count = len(files_written)

        if file_count == 0:
            logger.warning(
                "documentation_engine_no_files_produced task_id=%s",
                request.task_id,
            )
            return ExecutionResult(
                task_id=request.task_id,
                decision="failed",
                summary="Document writer produced no output files.",
                execution_agent_sequence=["context_selection_agent", "document_writer_agent"],
                evidence=state.evidence,
            ), request

        completed_scope = ", ".join(f.path for f in files_written)

        logger.info(
            "documentation_engine_completed task_id=%s files_written=%s",
            request.task_id,
            file_count,
        )

        return ExecutionResult(
            task_id=request.task_id,
            decision="completed",
            summary=f"Documentation generated: {file_count} file(s) written.",
            completed_scope=completed_scope,
            execution_agent_sequence=["context_selection_agent", "document_writer_agent"],
            evidence=state.evidence,
        ), request
