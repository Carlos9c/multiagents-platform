"""QA API endpoints.

POST /qa/{project_id}/run
    Starts a QA run in the background and immediately returns the
    qa_session_id so the client can poll for status.

POST /projects/{project_id}/conversations/notify-qa-completed
    Internal endpoint called once the background QA run finishes (used in
    test scenarios or when the runner is hosted in a separate process).
    Routes the completion event through Aria and broadcasts to WebSocket
    clients.

GET /qa/{project_id}/sessions
    Returns the QA session history for a project (latest first).

GET /qa/{project_id}/sessions/{qa_session_id}
    Returns a single QA session with its findings.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.ws.connection_manager import manager
from app.api.ws.protocol import assistant_message
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
from app.services.qa.qa_runner import create_qa_session, run_qa_background

logger = logging.getLogger(__name__)

router = APIRouter(tags=["qa"])

# Background executor for QA runs (isolated from the workflow executor).
_qa_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="qa_runner")


# ── Request / Response schemas ────────────────────────────────────────────────


class RunQAResponse(BaseModel):
    qa_session_id: int
    status: str
    project_id: int


class NotifyQACompletedRequest(BaseModel):
    conversation_id: int
    verdict: str
    has_findings: bool
    critical_count: int = 0
    high_count: int = 0
    findings_summary: dict = {}
    error_message: str | None = None


class NotifyQACompletedResponse(BaseModel):
    message: str
    broadcast_sent: bool


class QAFindingResponse(BaseModel):
    id: int
    finding_id: str
    severity: str
    category: str
    title: str
    description: str
    affected_component: str | None = None
    auto_remediable: bool
    remediation_hint: str | None = None
    producer_agent: str | None = None


class QASessionResponse(BaseModel):
    id: int
    project_id: int
    status: str
    product_type: str | None = None
    verdict: str | None = None
    agents_called: list[str] | None = None
    findings_summary: dict | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    findings: list[QAFindingResponse] = []


# ── POST /qa/{project_id}/run ─────────────────────────────────────────────────


@router.post("/qa/{project_id}/run", response_model=RunQAResponse)
def run_qa(
    project_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RunQAResponse:
    """Start a QA run for *project_id* in the background.

    Creates a ``QASession`` record synchronously (so the caller gets the
    ``qa_session_id`` right away), then fires the full run in a thread-pool
    worker.
    """
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    qa_session = create_qa_session(db, project_id)
    db.commit()

    background_tasks.add_task(_submit_qa_background, project_id, qa_session.id)

    return RunQAResponse(
        qa_session_id=qa_session.id,
        status=QA_SESSION_STATUS_RUNNING,
        project_id=project_id,
    )


def _submit_qa_background(project_id: int, qa_session_id: int) -> None:
    """Submit QA run to the thread-pool executor.

    BackgroundTasks runs in the same event-loop thread, so we hand off to a
    dedicated executor to avoid blocking the server.
    """
    _qa_executor.submit(run_qa_background, project_id, qa_session_id)


# ── POST /projects/{project_id}/conversations/notify-qa-completed ─────────────


@router.post(
    "/projects/{project_id}/conversations/notify-qa-completed",
    response_model=NotifyQACompletedResponse,
)
def notify_qa_completed(
    project_id: int,
    payload: NotifyQACompletedRequest,
    db: Session = Depends(get_db),
) -> NotifyQACompletedResponse:
    """Notify Aria that a QA run has completed.

    This endpoint is useful when the QA runner is invoked from a context that
    cannot directly call the runner's background function (e.g. a separate
    worker process or integration tests).

    The event is routed through the Aria orchestrator which decides whether to
    present a "qa_completed_no_issues" or "qa_completed_with_findings" message.
    """
    event_data = {
        "verdict": payload.verdict,
        "has_findings": payload.has_findings,
        "critical_count": payload.critical_count,
        "high_count": payload.high_count,
        "findings_summary": payload.findings_summary,
        "error_message": payload.error_message,
    }

    try:
        resp = process_with_pre_transitions(
            db,
            payload.conversation_id,
            AriaInput(
                source="system",
                system_event=SystemEvent(
                    event_type=SystemEventType.QA_COMPLETED,
                    data=event_data,
                ),
            ),
        )
    except AriaOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    return NotifyQACompletedResponse(
        message=resp.message,
        broadcast_sent=manager.has_connections(project_id),
    )


# ── GET /qa/{project_id}/sessions ─────────────────────────────────────────────


@router.get("/qa/{project_id}/sessions", response_model=list[QASessionResponse])
def list_qa_sessions(
    project_id: int,
    limit: int = 10,
    db: Session = Depends(get_db),
) -> list[QASessionResponse]:
    """Return the QA session history for a project (latest first)."""
    sessions = (
        db.query(QASessionModel)
        .filter(QASessionModel.project_id == project_id)
        .order_by(QASessionModel.id.desc())
        .limit(limit)
        .all()
    )
    return [_session_to_response(s, include_findings=False) for s in sessions]


# ── GET /qa/{project_id}/sessions/{qa_session_id} ─────────────────────────────


@router.get(
    "/qa/{project_id}/sessions/{qa_session_id}",
    response_model=QASessionResponse,
)
def get_qa_session(
    project_id: int,
    qa_session_id: int,
    db: Session = Depends(get_db),
) -> QASessionResponse:
    """Return a single QA session including its findings."""
    session = db.get(QASessionModel, qa_session_id)
    if session is None or session.project_id != project_id:
        raise HTTPException(
            status_code=404,
            detail=f"QA session {qa_session_id} not found for project {project_id}",
        )
    return _session_to_response(session, include_findings=True)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _session_to_response(
    session: QASessionModel,
    *,
    include_findings: bool,
) -> QASessionResponse:
    findings_summary = session.findings_summary
    if isinstance(findings_summary, str):
        import json

        try:
            findings_summary = json.loads(findings_summary)
        except Exception:
            findings_summary = {}

    findings: list[QAFindingResponse] = []
    if include_findings:
        findings = [
            QAFindingResponse(
                id=f.id,
                finding_id=f.finding_id,
                severity=f.severity,
                category=f.category,
                title=f.title,
                description=f.description,
                affected_component=f.affected_component,
                auto_remediable=f.auto_remediable,
                remediation_hint=f.remediation_hint,
                producer_agent=f.producer_agent,
            )
            for f in (session.findings or [])
        ]

    return QASessionResponse(
        id=session.id,
        project_id=session.project_id,
        status=session.status,
        product_type=session.product_type,
        verdict=session.verdict,
        agents_called=session.agents_called,
        findings_summary=findings_summary,
        duration_seconds=session.duration_seconds,
        error_message=session.error_message,
        findings=findings,
    )
