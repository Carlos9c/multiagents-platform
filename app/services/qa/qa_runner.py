"""QARunner — top-level service for launching a QA run from the API layer.

Responsibilities
----------------
1. Create a ``QASession`` DB record (status=running) before the run starts.
2. Resolve project metadata (product_type, project_goal, source_path).
3. Build a ``QARequest`` and delegate to ``QAEngine.run()``.
4. Persist individual ``QAFinding`` rows from the engine result.
5. Store the serialised ``QAResult`` in ``conversation.pending_qa_report``.
6. Notify Aria via ``process_with_pre_transitions`` with
   ``SystemEventType.QA_COMPLETED``.
7. Broadcast the notification over WebSocket.

The runner is invoked from a background thread that has its *own* DB session —
the same threading model used by ``_run_workflow_background`` in the WS router.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.api.ws.connection_manager import manager
from app.api.ws.protocol import assistant_message
from app.db.session import SessionLocal
from app.models.conversation import (
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.project import Project
from app.models.qa_finding import QAFinding as QAFindingModel
from app.models.qa_session import (
    QA_SESSION_STATUS_RUNNING,
)
from app.models.qa_session import (
    QASession as QASessionModel,
)
from app.services.conversation.aria import (
    AriaInput,
    AriaOrchestratorError,
    SystemEvent,
    SystemEventType,
    process_with_pre_transitions,
)
from app.services.project_storage import ProjectStorageService
from app.services.qa.contracts import QARequest, QAResult
from app.services.qa.qa_engine import QAEngine

logger = logging.getLogger(__name__)


# ── Public entry points ────────────────────────────────────────────────────────


def create_qa_session(db: Session, project_id: int) -> QASessionModel:
    """Create and flush a QASession record in *running* status.

    Called synchronously from the API endpoint so the caller can return the
    ``qa_session_id`` immediately to the client.
    """
    project = db.get(Project, project_id)
    product_type = (project.product_type if project else None) or "unknown"

    session = QASessionModel(
        project_id=project_id,
        triggered_by="user",
        status=QA_SESSION_STATUS_RUNNING,
        product_type=product_type,
        created_at=datetime.now(timezone.utc),
    )
    db.add(session)
    db.flush()
    return session


def run_qa_background(project_id: int, qa_session_id: int) -> None:
    """Execute a complete QA run inside a background thread.

    Acquires its own ``SessionLocal`` DB session, runs the engine, persists
    findings, then notifies Aria and broadcasts over WebSocket.
    """
    db = SessionLocal()
    try:
        _run(db, project_id, qa_session_id)
    except Exception:
        logger.exception(
            "qa_runner_background_error project_id=%s qa_session_id=%s",
            project_id,
            qa_session_id,
        )
    finally:
        db.close()


# ── Internal implementation ────────────────────────────────────────────────────


def _run(db: Session, project_id: int, qa_session_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None:
        logger.error("qa_runner_project_not_found project_id=%s", project_id)
        return

    request = _build_request(db, project, qa_session_id)

    engine = QAEngine()
    result: QAResult = engine.run(db=db, request=request)

    _persist_findings(db, qa_session_id, result)
    db.commit()

    _notify_aria(db, project_id, result)


def _build_request(db: Session, project: Project, qa_session_id: int) -> QARequest:
    """Build a ``QARequest`` from the project's stored metadata."""
    storage = ProjectStorageService()
    paths = storage.get_project_paths(project.id)
    source_path = str(paths.source_dir)

    product_type = project.product_type or "unknown"
    project_goal = project.description or project.name or f"Project {project.id}"

    return QARequest(
        project_id=project.id,
        qa_session_id=qa_session_id,
        product_type=product_type,
        project_goal=project_goal,
        source_path=source_path,
        workspace_path=source_path,
    )


def _persist_findings(
    db: Session,
    qa_session_id: int,
    result: QAResult,
) -> None:
    """Insert a ``QAFinding`` row for every finding in *result*."""
    for finding in result.findings:
        row = QAFindingModel(
            qa_session_id=qa_session_id,
            finding_id=finding.finding_id,
            severity=finding.severity,
            category=finding.category,
            title=finding.title,
            description=finding.description,
            reproduction_steps=finding.reproduction_steps or [],
            evidence=finding.evidence or {},
            affected_component=finding.affected_component,
            auto_remediable=finding.auto_remediable,
            remediation_hint=finding.remediation_hint,
            producer_agent=finding.producer_agent,
        )
        db.add(row)


def _notify_aria(db: Session, project_id: int, result: QAResult) -> None:
    """Route the QA completion through Aria and broadcast over WebSocket."""
    active_conv = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if active_conv is None:
        logger.warning(
            "qa_runner_no_active_conv project_id=%s — skipping Aria notification",
            project_id,
        )
        return

    # Store the serialised result so Aria can read it when the user responds.
    active_conv.pending_qa_report = result.model_dump_json()
    db.add(active_conv)
    db.flush()

    event_data: dict = {
        "verdict": result.verdict,
        "has_findings": result.has_findings,
        "critical_count": result.critical_count,
        "high_count": result.high_count,
        "findings_summary": result.findings_summary(),
        "remediation_tasks": [t.model_dump() for t in result.remediation_tasks],
        "error_message": result.error_message,
    }

    try:
        resp = process_with_pre_transitions(
            db,
            active_conv.id,
            AriaInput(
                source="system",
                system_event=SystemEvent(
                    event_type=SystemEventType.QA_COMPLETED,
                    data=event_data,
                ),
            ),
        )
        db.commit()
        manager.broadcast_sync(
            project_id,
            assistant_message(
                content=resp.message,
                event=resp.event,
                phase=resp.phase,
                conversation_id=resp.conversation_id,
                project_id=resp.project_id,
            ),
        )
    except AriaOrchestratorError as exc:
        logger.warning(
            "qa_runner_aria_notify_failed project_id=%s error=%s",
            project_id,
            exc,
        )
