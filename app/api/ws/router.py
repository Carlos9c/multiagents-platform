"""WebSocket and conversation REST endpoints.

WS:   ws://host/ws/projects/{project_id}/chat
REST: POST /projects/{project_id}/conversations/confirm-start
      POST /projects/{project_id}/conversations/notify-review
      POST /projects/{project_id}/conversations/notify-task-event
      POST /projects/{project_id}/conversations/pause
      POST /projects/{project_id}/conversations/resume-workflow
      GET  /projects/{project_id}/conversations/active
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.ws.connection_manager import manager
from app.api.ws.protocol import (
    MessageRecord,
    assistant_message,
    conversation_state,
    error_message,
    execution_event,
    pong,
    review_required,
    workflow_paused,
)
from app.db.session import SessionLocal
from app.models.conversation import (
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.models.execution_run import ExecutionRun
from app.models.task import (
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.services.conversation.aria import (
    AriaInput,
    AriaOrchestratorError,
    AriaResponse,
    SystemEvent,
    SystemEventType,
    check_project_completed,
    get_or_create_conversation,
    process_with_pre_transitions,
)
from app.services.project_start_service import ActiveTasksError
from app.services.project_workflow_service import (
    ProjectWorkflowServiceError,
    clear_workflow_stop,
    request_workflow_stop,
    run_project_workflow,
    teardown_project_environment,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation"])

_workflow_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="workflow")


def _find_active_conversation(db: Session, project_id: int) -> Conversation | None:
    return (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )


def _get_task_validation_notes(db: Session, task_id: int) -> str | None:
    last_run = (
        db.query(ExecutionRun)
        .filter(ExecutionRun.task_id == task_id)
        .order_by(ExecutionRun.id.desc())
        .first()
    )
    if last_run is None:
        return None
    parts = [last_run.validation_notes, last_run.blockers_found, last_run.error_message]
    combined = " | ".join(p for p in parts if p)
    return combined or None


def _build_workflow_failure_reason(result) -> str:
    base = result.notes or "El flujo de trabajo se ha detenido sin motivo registrado."
    if result.iterations:
        last_signals = result.iterations[-1].decision_signals or []
        if last_signals:
            base = f"{base} (señal: {', '.join(last_signals)})"
    return base


def _broadcast_aria_response(project_id: int, resp: AriaResponse) -> None:
    """Helper to broadcast an AriaResponse to connected WebSocket clients."""
    manager.broadcast_sync(
        project_id,
        assistant_message(
            content=resp.message,
            event=resp.event,
            phase=resp.phase,
            conversation_id=resp.conversation_id,
            project_id=resp.project_id,
            requirements_draft=resp.requirements_draft,
        ),
    )


def _run_workflow_background(project_id: int) -> None:
    """Run the full project workflow in a background thread with its own DB session."""
    db = SessionLocal()
    try:
        result = run_project_workflow(db, project_id, auto_teardown=False)

        # ── Paused by user ────────────────────────────────────────────────────
        if result.status == "paused":
            active_conv = _find_active_conversation(db, project_id)
            if active_conv is not None:
                active_conv.phase = CONVERSATION_PHASE_PAUSED
                db.commit()
                manager.broadcast_sync(
                    project_id,
                    workflow_paused(
                        conversation_id=active_conv.id,
                        project_id=project_id,
                    ),
                )
            return

        # ── Blocked for manual review (task in awaiting_review) ───────────────
        awaiting_task = (
            db.query(Task)
            .filter(Task.project_id == project_id, Task.status == TASK_STATUS_AWAITING_REVIEW)
            .order_by(Task.id.desc())
            .first()
        )
        if awaiting_task is not None:
            active_conv = _find_active_conversation(db, project_id)
            if active_conv is not None:
                validation_notes = _get_task_validation_notes(db, awaiting_task.id)
                try:
                    resp = process_with_pre_transitions(
                        db,
                        active_conv.id,
                        AriaInput(
                            source="system",
                            system_event=SystemEvent(
                                event_type=SystemEventType.MANUAL_REVIEW_REQUIRED,
                                data={
                                    "task_id": awaiting_task.id,
                                    "task_title": awaiting_task.title,
                                    "task_description": awaiting_task.description,
                                    "validation_notes": validation_notes,
                                },
                            ),
                        ),
                    )
                    manager.broadcast_sync(
                        project_id,
                        review_required(
                            content=resp.message,
                            task_id=awaiting_task.id,
                            conversation_id=active_conv.id,
                        ),
                    )
                except AriaOrchestratorError as exc:
                    logger.warning("review_notify_failed project_id=%s error=%s", project_id, exc)
            return

        # ── Manual review required (task failed/partial, or project-level) ────
        if result.manual_review_required:
            failed_task = (
                db.query(Task)
                .filter(
                    Task.project_id == project_id,
                    Task.status.in_([TASK_STATUS_FAILED, TASK_STATUS_PARTIAL]),
                )
                .order_by(Task.id.desc())
                .first()
            )
            active_conv = _find_active_conversation(db, project_id)
            if active_conv is None:
                return

            if failed_task is not None:
                validation_notes = _get_task_validation_notes(db, failed_task.id)
                try:
                    resp = process_with_pre_transitions(
                        db,
                        active_conv.id,
                        AriaInput(
                            source="system",
                            system_event=SystemEvent(
                                event_type=SystemEventType.MANUAL_REVIEW_REQUIRED,
                                data={
                                    "task_id": failed_task.id,
                                    "task_title": failed_task.title,
                                    "task_description": failed_task.description,
                                    "validation_notes": validation_notes,
                                },
                            ),
                        ),
                    )
                    manager.broadcast_sync(
                        project_id,
                        review_required(
                            content=resp.message,
                            task_id=failed_task.id,
                            conversation_id=active_conv.id,
                        ),
                    )
                except AriaOrchestratorError as exc:
                    logger.warning("review_notify_failed project_id=%s error=%s", project_id, exc)
            else:
                # No task anchor — project-level failure
                failure_reason = _build_workflow_failure_reason(result)
                logger.warning(
                    "workflow_project_level_review project_id=%s reason=%s",
                    project_id,
                    failure_reason,
                )
                _try_notify_workflow_error(db, project_id, failure_reason, active_conv)
            return

        # ── Project completed ──────────────────────────────────────────────────
        completion = check_project_completed(db, project_id)
        if completion:
            _broadcast_aria_response(project_id, completion)

    except ProjectWorkflowServiceError as exc:
        logger.error("Workflow error for project %s: %s", project_id, exc)
        _try_notify_workflow_error(db, project_id, str(exc))
    except Exception as exc:
        logger.exception("Unexpected workflow error for project %s: %s", project_id, exc)
        _try_notify_workflow_error(db, project_id, str(exc))
    finally:
        teardown_project_environment(project_id)
        db.close()


def _try_notify_workflow_error(
    db: Session,
    project_id: int,
    failure_reason: str,
    active_conv: Conversation | None = None,
) -> None:
    """Best-effort: route a workflow error through Aria."""
    try:
        if active_conv is None:
            active_conv = _find_active_conversation(db, project_id)
        if active_conv is None:
            logger.warning(
                "workflow_error_no_conv project_id=%s — no active conversation to notify",
                project_id,
            )
            return
        resp = process_with_pre_transitions(
            db,
            active_conv.id,
            AriaInput(
                source="system",
                system_event=SystemEvent(
                    event_type=SystemEventType.WORKFLOW_ERROR,
                    data={"failure_reason": failure_reason},
                ),
            ),
        )
        manager.broadcast_sync(
            project_id,
            review_required(
                content=resp.message,
                task_id=None,
                conversation_id=active_conv.id,
            ),
        )
    except Exception as notify_exc:  # noqa: BLE001
        logger.warning(
            "workflow_error_notify_failed project_id=%s error=%s", project_id, notify_exc
        )


_HISTORY_LIMIT = 100


# ── WebSocket endpoint ────────────────────────────────────────────────────────


@router.websocket("/ws/projects/{project_id}/chat")
async def websocket_chat(
    websocket: WebSocket,
    project_id: int,
    db: Session = Depends(get_db),
) -> None:
    await manager.connect(websocket, project_id)

    try:
        conversation = get_or_create_conversation(db, project_id)
        await _send_conversation_state(websocket, db, conversation)

        while True:
            try:
                data = await websocket.receive_json()
            except Exception:
                break

            msg_type = data.get("type")

            if msg_type == "ping":
                await websocket.send_json(pong())
                continue

            if msg_type != "user_message":
                await websocket.send_json(error_message(f"Unknown message type: {msg_type}"))
                continue

            content = (data.get("content") or "").strip()
            if not content:
                continue

            try:
                response = process_with_pre_transitions(
                    db,
                    conversation.id,
                    AriaInput(source="user", user_message=content),
                )
            except (AriaOrchestratorError, ActiveTasksError) as exc:
                await websocket.send_json(error_message(str(exc)))
                continue

            await websocket.send_json(
                assistant_message(
                    content=response.message,
                    event=response.event,
                    phase=response.phase,
                    conversation_id=conversation.id,
                    project_id=response.project_id,
                    requirements_draft=response.requirements_draft,
                )
            )

            if response.event == "project_started" and response.project_id:
                _workflow_executor.submit(_run_workflow_background, response.project_id)

            if response.event == "review_resolved" and response.project_id:
                _workflow_executor.submit(_run_workflow_background, response.project_id)

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, project_id)


# ── REST: notify review (from execution layer) ────────────────────────────────


class NotifyReviewRequest(BaseModel):
    task_id: int
    conversation_id: int


class NotifyReviewResponse(BaseModel):
    message: str
    broadcast_sent: bool


@router.post(
    "/projects/{project_id}/conversations/notify-review",
    response_model=NotifyReviewResponse,
)
def notify_review(
    project_id: int,
    payload: NotifyReviewRequest,
    db: Session = Depends(get_db),
) -> NotifyReviewResponse:
    """Called by the execution layer when a task enters AWAITING_REVIEW status."""
    task = db.get(Task, payload.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {payload.task_id} not found")

    validation_notes = _get_task_validation_notes(db, payload.task_id)

    try:
        response = process_with_pre_transitions(
            db,
            payload.conversation_id,
            AriaInput(
                source="system",
                system_event=SystemEvent(
                    event_type=SystemEventType.MANUAL_REVIEW_REQUIRED,
                    data={
                        "task_id": task.id,
                        "task_title": task.title,
                        "task_description": task.description,
                        "validation_notes": validation_notes,
                    },
                ),
            ),
        )
    except AriaOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    manager.broadcast_sync(
        project_id,
        review_required(
            content=response.message,
            task_id=payload.task_id,
            conversation_id=payload.conversation_id,
        ),
    )

    return NotifyReviewResponse(
        message=response.message,
        broadcast_sent=manager.has_connections(project_id),
    )


# ── REST: notify task event ───────────────────────────────────────────────────


class NotifyTaskEventRequest(BaseModel):
    task_id: int
    task_title: str
    status: str


@router.post("/projects/{project_id}/conversations/notify-task-event")
def notify_task_event(
    project_id: int,
    payload: NotifyTaskEventRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Pushes a task status update to connected WebSocket clients."""
    broadcast_payload = execution_event(
        task_id=payload.task_id,
        task_title=payload.task_title,
        status=payload.status,
        project_id=project_id,
    )
    manager.broadcast_sync(project_id, broadcast_payload)

    if payload.status in TERMINAL_TASK_STATUSES:
        completion = check_project_completed(db, project_id)
        if completion:
            _broadcast_aria_response(project_id, completion)

    return {"broadcast_sent": manager.has_connections(project_id)}


# ── REST: confirm project start ───────────────────────────────────────────────


class ConfirmStartRequest(BaseModel):
    conversation_id: int
    name: str | None = None
    description: str | None = None
    source_path: str | None = None
    manual_update: bool = True


@router.post("/projects/{project_id}/conversations/confirm-start")
def confirm_start(
    project_id: int,
    payload: ConfirmStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Called when the user clicks the Start button in the UI."""
    from app.models.project import Project

    # Update project metadata directly (before routing through Aria)
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    if payload.name:
        project.name = payload.name
    if payload.description:
        project.description = payload.description
    db.add(project)
    db.flush()

    try:
        response = process_with_pre_transitions(
            db,
            payload.conversation_id,
            AriaInput(
                source="system",
                system_event=SystemEvent(
                    event_type=SystemEventType.CONFIRM_START,
                    data={
                        "source_path": payload.source_path,
                        "manual_update": payload.manual_update,
                    },
                ),
            ),
        )
    except AriaOrchestratorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    manager.broadcast_sync(
        project_id,
        assistant_message(
            content=response.message,
            event=response.event,
            phase=response.phase,
            conversation_id=payload.conversation_id,
            project_id=project_id,
        ),
    )

    if response.event == "project_started":
        background_tasks.add_task(_run_workflow_background, project_id)

    return {
        "message": response.message,
        "phase": response.phase,
        "project_id": project_id,
    }


# ── REST: active conversation state ──────────────────────────────────────────


class ConversationStateResponse(BaseModel):
    conversation_id: int
    phase: str
    status: str
    review_episode_attempts: int
    requirements_draft: str | None
    requirements_ready: bool
    messages: list[MessageRecord]


@router.get(
    "/projects/{project_id}/conversations/active",
    response_model=ConversationStateResponse,
)
def get_active_conversation(
    project_id: int,
    db: Session = Depends(get_db),
) -> ConversationStateResponse:
    """Returns the current active conversation state for a project."""
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="No active conversation for this project")

    messages = _load_messages(db, conversation.id)
    return ConversationStateResponse(
        conversation_id=conversation.id,
        phase=conversation.phase,
        status=conversation.status,
        review_episode_attempts=conversation.review_episode_attempts,
        requirements_draft=conversation.requirements_draft,
        requirements_ready=conversation.requirements_ready,
        messages=messages,
    )


# ── REST: pause workflow ──────────────────────────────────────────────────────


@router.post("/projects/{project_id}/conversations/pause")
def pause_workflow(
    project_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """Signal the workflow loop to stop after the current task finishes."""
    request_workflow_stop(project_id)
    manager.broadcast_sync(
        project_id, {"type": "workflow_pause_requested", "project_id": project_id}
    )
    return {"status": "pause_requested", "project_id": project_id}


# ── REST: resume workflow ─────────────────────────────────────────────────────


class ResumeWorkflowRequest(BaseModel):
    conversation_id: int | None = None


@router.post("/projects/{project_id}/conversations/resume-workflow")
def resume_workflow(
    project_id: int,
    payload: ResumeWorkflowRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Resume a paused or interrupted workflow."""
    clear_workflow_stop(project_id)

    active_conv = _find_active_conversation(db, project_id)
    if active_conv is None:
        return {"status": "no_active_conversation", "project_id": project_id}

    if active_conv.phase not in (CONVERSATION_PHASE_PAUSED, CONVERSATION_PHASE_EXECUTING):
        return {
            "status": "not_resumable",
            "phase": active_conv.phase,
            "project_id": project_id,
        }

    has_pending = (
        db.query(Task)
        .filter(Task.project_id == project_id, Task.status == TASK_STATUS_PENDING)
        .first()
    ) is not None

    if not has_pending:
        return {"status": "no_pending_tasks", "project_id": project_id}

    active_conv.phase = CONVERSATION_PHASE_EXECUTING
    db.commit()

    _workflow_executor.submit(_run_workflow_background, project_id)

    manager.broadcast_sync(
        project_id,
        {"type": "workflow_resumed", "project_id": project_id, "phase": "executing"},
    )

    return {"status": "resumed", "project_id": project_id}


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _send_conversation_state(
    websocket: WebSocket,
    db: Session,
    conversation: Conversation,
) -> None:
    messages = _load_messages(db, conversation.id)
    payload = conversation_state(
        conversation_id=conversation.id,
        phase=conversation.phase,
        messages=messages,
        requirements_draft=conversation.requirements_draft,
    )
    await websocket.send_json(payload)


def _load_messages(db: Session, conversation_id: int) -> list[MessageRecord]:
    rows = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.id.asc())
        .limit(_HISTORY_LIMIT)
        .all()
    )
    return [MessageRecord(role=m.role, content=m.content, task_id=None) for m in rows]
