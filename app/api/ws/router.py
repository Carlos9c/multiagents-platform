"""WebSocket and conversation REST endpoints.

WS:   ws://host/ws/projects/{project_id}/chat
REST: POST /projects/{project_id}/conversations/notify-review
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
from app.models.task import (
    TASK_STATUS_AWAITING_REVIEW,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_PENDING,
    TERMINAL_TASK_STATUSES,
    Task,
)
from app.services.conversation.project_assistant import (
    ProjectAssistantError,
    get_or_create_conversation,
    notify_project_completed,
    notify_review_started,
    notify_workflow_error,
    process_user_message,
    start_project_manually,
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


def _find_active_conversation(db, project_id: int) -> "Conversation | None":
    return (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )


def _try_notify_workflow_error(db, project_id: int, failure_reason: str) -> None:
    """
    Best-effort: find the active conversation and call notify_workflow_error so Aria
    can inform the user exactly what happened and how to continue. Swallows all errors
    so we never mask the original exception or crash the background thread.
    """
    try:
        active_conv = _find_active_conversation(db, project_id)
        if active_conv is None:
            logger.warning(
                "workflow_error_no_conv project_id=%s — no active conversation to notify",
                project_id,
            )
            return
        resp = notify_workflow_error(
            db, conversation_id=active_conv.id, failure_reason=failure_reason
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


def _build_workflow_failure_reason(result) -> str:
    """
    Derive a human-readable failure reason from a ProjectWorkflowResult so Aria
    can explain exactly what stopped the workflow and what recovery options exist.
    Includes the last iteration's decision signal for precision.
    """
    base = result.notes or "El flujo de trabajo se ha detenido sin motivo registrado."

    # Append the last iteration's decision signal if present for extra precision
    if result.iterations:
        last_signals = result.iterations[-1].decision_signals or []
        if last_signals:
            signal_str = ", ".join(last_signals)
            base = f"{base} (señal: {signal_str})"

    return base


def _run_workflow_background(project_id: int) -> None:
    """Run the full project workflow in a background thread with its own DB session.

    Teardown of the Docker container is intentionally deferred to the finally block so
    the conversation transition to AWAITING_REVIEW (or PAUSED) is broadcast to the client
    before the container disappears. This eliminates the window where the user could send
    a message and receive "El proyecto está en ejecución" while teardown is in progress.
    """
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

        # ── Blocked for manual review (task explicitly in awaiting_review) ────
        awaiting_task = (
            db.query(Task)
            .filter(Task.project_id == project_id, Task.status == TASK_STATUS_AWAITING_REVIEW)
            .order_by(Task.id.desc())
            .first()
        )
        if awaiting_task is not None:
            active_conv = _find_active_conversation(db, project_id)
            if active_conv is not None:
                try:
                    resp = notify_review_started(
                        db, conversation_id=active_conv.id, task_id=awaiting_task.id
                    )
                    manager.broadcast_sync(
                        project_id,
                        review_required(
                            content=resp.message,
                            task_id=awaiting_task.id,
                            conversation_id=active_conv.id,
                        ),
                    )
                except ProjectAssistantError as exc:
                    logger.warning("review_notify_failed project_id=%s error=%s", project_id, exc)
            return

        # ── Manual review required ─────────────────────────────────────────────
        # Covers three sub-cases in order of preference:
        #   1. Task ended FAILED/PARTIAL (validator chose manual_review but set FAILED status)
        #   2. Project-level failure — no task anchor at all (env error, empty plan, etc.)
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
            if failed_task is not None:
                active_conv = _find_active_conversation(db, project_id)
                if active_conv is not None:
                    try:
                        resp = notify_review_started(
                            db, conversation_id=active_conv.id, task_id=failed_task.id
                        )
                        manager.broadcast_sync(
                            project_id,
                            review_required(
                                content=resp.message,
                                task_id=failed_task.id,
                                conversation_id=active_conv.id,
                            ),
                        )
                    except ProjectAssistantError as exc:
                        logger.warning(
                            "review_notify_failed project_id=%s error=%s", project_id, exc
                        )
            else:
                # No task anchor — project-level failure (env, empty plan, iteration limit).
                # Notify Aria so the user learns exactly what happened and how to continue.
                failure_reason = _build_workflow_failure_reason(result)
                logger.warning(
                    "workflow_project_level_review project_id=%s reason=%s",
                    project_id,
                    failure_reason,
                )
                _try_notify_workflow_error(db, project_id, failure_reason)
            return

        # ── Project completed ──────────────────────────────────────────────────
        completion = notify_project_completed(db, project_id)
        if completion:
            manager.broadcast_sync(
                project_id,
                assistant_message(
                    content=completion.message,
                    event=completion.event,
                    phase=completion.phase,
                    conversation_id=completion.conversation_id,
                    project_id=project_id,
                ),
            )
    except ProjectWorkflowServiceError as exc:
        logger.error("Workflow error for project %s: %s", project_id, exc)
        _try_notify_workflow_error(db, project_id, str(exc))
    except Exception as exc:
        logger.exception("Unexpected workflow error for project %s: %s", project_id, exc)
        _try_notify_workflow_error(db, project_id, str(exc))
    finally:
        # Container teardown always happens here — after all Aria notifications — so the
        # conversation is already in AWAITING_REVIEW/PAUSED before the container stops.
        teardown_project_environment(project_id)
        db.close()


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
        # Sync to DB: get or create conversation and send current state
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
                response = process_user_message(
                    db,
                    conversation_id=conversation.id,
                    user_message=content,
                )
            except (ProjectAssistantError, ActiveTasksError) as exc:
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


# ── REST: push review notification from execution layer ───────────────────────


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
    """
    Called by the execution layer when a task enters AWAITING_REVIEW status.
    Updates the conversation phase and pushes a review_required event to
    any connected WebSocket clients.
    """
    try:
        response = notify_review_started(
            db,
            conversation_id=payload.conversation_id,
            task_id=payload.task_id,
        )
    except ProjectAssistantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    broadcast_payload = review_required(
        content=response.message,
        task_id=payload.task_id,
        conversation_id=payload.conversation_id,
    )
    manager.broadcast_sync(project_id, broadcast_payload)

    return NotifyReviewResponse(
        message=response.message,
        broadcast_sent=manager.has_connections(project_id),
    )


# ── REST: push execution event from workflow layer ────────────────────────────


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
    """
    Pushes a task status update to connected WebSocket clients.
    Called after task execution completes (completed, failed, partial).
    Also checks for project completion and broadcasts Aria's closing message.
    """
    broadcast_payload = execution_event(
        task_id=payload.task_id,
        task_title=payload.task_title,
        status=payload.status,
        project_id=project_id,
    )
    manager.broadcast_sync(project_id, broadcast_payload)

    if payload.status in TERMINAL_TASK_STATUSES:
        completion = notify_project_completed(db, project_id)
        if completion:
            manager.broadcast_sync(
                project_id,
                assistant_message(
                    content=completion.message,
                    event=completion.event,
                    phase=completion.phase,
                    conversation_id=completion.conversation_id,
                    project_id=project_id,
                ),
            )

    return {"broadcast_sent": manager.has_connections(project_id)}


# ── REST: confirm project start (called by the Start button) ─────────────────


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
    """
    Called when the user clicks the Start button in the UI.
    Updates project details, kicks off planning, and runs the full workflow in background.
    """
    try:
        response = start_project_manually(
            db,
            conversation_id=payload.conversation_id,
            name=payload.name,
            description=payload.description,
            source_path=payload.source_path,
            manual_update=payload.manual_update,
        )
    except ProjectAssistantError as exc:
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
        messages=messages,
    )


# ── REST: pause workflow ──────────────────────────────────────────────────────


@router.post("/projects/{project_id}/conversations/pause")
def pause_workflow(
    project_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """
    Signal the workflow loop to stop after the current task finishes.
    The loop stops cooperatively — the task in progress is not interrupted.
    """
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
    """
    Resume a paused or interrupted workflow.

    Works for:
    - Manually paused projects (phase=paused)
    - Projects stuck in executing after a server crash (phase=executing, no active thread)
    - Projects after a review is resolved via REST (edge case)
    """
    clear_workflow_stop(project_id)

    active_conv = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )

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
    return [MessageRecord(role=m.role, content=m.content, task_id=m.task_id) for m in rows]
