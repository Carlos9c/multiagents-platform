"""Aria Conversation Orchestrator.

Aria is the conversational interface of the system. On each turn she:
  1. Builds a lightweight project snapshot from DB (no LLM).
  2. Persists the incoming input as a message (user or system role).
  3. Runs a lightweight loop (max MAX_STEPS iterations):
       - Calls the Aria LLM to get an AriaStep decision.
       - If action="respond": persists the response, applies phase transitions, returns.
       - If action="call_tool": executes the tool, appends result, loops.
       - No tool may be called twice in the same turn (no-repeat constraint).
  4. If the loop exhausts MAX_STEPS without a "respond" action, forces a fallback response.

Phase transitions are the orchestrator's responsibility — tools return structured data
only and never mutate conversation.phase or other state fields directly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_COMPLETED,
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_PHASE_QA_RUNNING,
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_SYSTEM,
    MESSAGE_ROLE_USER,
    Conversation,
    ConversationMessage,
)
from app.models.task import PLANNING_LEVEL_ATOMIC, TERMINAL_TASK_STATUSES, Task
from app.services.conversation.aria.context_builder import ProjectSnapshot, build_snapshot
from app.services.conversation.aria.contracts import (
    AriaInput,
    AriaOrchestratorError,
    AriaResponse,
    AriaStep,
    ProjectReviewContext,
    SystemEventType,
    TaskReviewContext,
    ToolName,
    ToolResult,
    classify_workflow_failure,
    review_context_from_json,
    review_context_to_json,
)
from app.services.conversation.aria.tools import (
    ConfirmationTool,
    QATool,
    QueryTool,
    RequirementsTool,
    ResumptionTool,
    ReviewTool,
    StartProjectTool,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

MAX_STEPS = 4
_HISTORY_LIMIT = 40  # messages sent to Aria's prompt


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = prompt_loader.get("aria_orchestrator")


# ── Public API ────────────────────────────────────────────────────────────────


def get_or_create_conversation(db: Session, project_id: int) -> Conversation:
    """Return the active conversation for this project, or create one with Aria's intro."""
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if conversation is None:
        conversation = Conversation(
            project_id=project_id,
            status=CONVERSATION_STATUS_ACTIVE,
            phase=CONVERSATION_PHASE_GATHERING,
            review_episode_attempts=0,
            requirements_ready=False,
        )
        db.add(conversation)
        db.flush()
        _append_message(
            db,
            conversation.id,
            MESSAGE_ROLE_ASSISTANT,
            "¡Hola! Soy Aria. Cuéntame qué proyecto quieres construir y te ayudaré a ponerlo en marcha.",
        )
        db.commit()
        db.refresh(conversation)
    return conversation


def process(
    db: Session,
    conversation_id: int,
    aria_input: AriaInput,
) -> AriaResponse:
    """Main entry point. Process a user message or system event through the Aria loop."""
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise AriaOrchestratorError(f"Conversation {conversation_id} not found")

    db.refresh(conversation)

    if conversation.status != CONVERSATION_STATUS_ACTIVE:
        raise AriaOrchestratorError(
            f"Conversation {conversation_id} is not active (status={conversation.status})"
        )

    snapshot = build_snapshot(db, conversation.project_id, conversation)

    # Persist the incoming input as a message
    if aria_input.source == "user" and aria_input.user_message:
        _append_message(db, conversation.id, MESSAGE_ROLE_USER, aria_input.user_message)
    elif aria_input.source == "system" and aria_input.system_event:
        system_text = _format_system_event(aria_input)
        _append_message(db, conversation.id, MESSAGE_ROLE_SYSTEM, system_text)

    # Run the orchestration loop
    return _run_loop(db, conversation, snapshot, aria_input)


# ── Orchestration loop ────────────────────────────────────────────────────────


def _run_loop(
    db: Session,
    conversation: Conversation,
    snapshot: ProjectSnapshot,
    aria_input: AriaInput,
) -> AriaResponse:
    tool_registry = _build_tool_registry()
    tools_called: set[ToolName] = set()
    tool_results: list[ToolResult] = []

    for step in range(MAX_STEPS):
        aria_step = _call_aria_llm(snapshot, conversation, aria_input, tool_results)

        if aria_step.action == "respond":
            snapshot = _apply_confirmation_resumption_guard(
                db, conversation, snapshot, tool_results, tools_called, tool_registry, step
            )
            return _finalise_response(
                db, conversation, snapshot, aria_step, tool_results, aria_input
            )

        # call_tool path
        tool_name = aria_step.tool_name
        if tool_name is None:
            logger.warning("aria_loop: action=call_tool but tool_name is None — forcing respond")
            break

        if tool_name in tools_called:
            logger.warning(
                "aria_loop: no-repeat constraint triggered for %s at step %d — forcing respond",
                tool_name,
                step,
            )
            break

        # Guard: start_project must never run in the same turn as requirements_agent.
        # When requirements_agent returns sufficient the user has not yet confirmed —
        # Aria must present the summary and wait for the next user turn or a
        # confirm_start system event.
        if tool_name == ToolName.START_PROJECT and ToolName.REQUIREMENTS in tools_called:
            logger.warning(
                "aria_loop: start_project blocked at step %d — requirements_agent ran "
                "in the same turn; forcing respond so user can confirm",
                step,
            )
            break

        tool = tool_registry.get(tool_name)
        if tool is None:
            logger.warning("aria_loop: unknown tool %s — forcing respond", tool_name)
            break

        logger.debug("aria_loop step=%d calling tool=%s", step, tool_name)
        result = tool.execute(db, conversation.project_id, conversation, aria_step.tool_hint)
        tools_called.add(tool_name)
        tool_results.append(result)

        # Refresh snapshot after tool execution (tools may have mutated DB)
        db.refresh(conversation)
        snapshot = build_snapshot(db, conversation.project_id, conversation)

    # Exhausted steps or broke out — apply the resumption guard before the forced reply
    snapshot = _apply_confirmation_resumption_guard(
        db, conversation, snapshot, tool_results, tools_called, tool_registry, -1
    )
    logger.warning("aria_loop: exhausted steps or broke early — forcing fallback respond")
    aria_step = _call_aria_llm(snapshot, conversation, aria_input, tool_results, force_respond=True)
    return _finalise_response(
        db, conversation, snapshot, aria_step, tool_results, aria_input, is_fallback=True
    )


# ── Resumption guard ─────────────────────────────────────────────────────────


def _apply_confirmation_resumption_guard(
    db: "Session",
    conversation: "Conversation",
    snapshot: "ProjectSnapshot",
    tool_results: "list[ToolResult]",
    tools_called: "set[ToolName]",
    tool_registry: "dict",
    step: int,
) -> "ProjectSnapshot":
    """Force resumption_agent when confirmation returned True but it hasn't run yet.

    Called just before any respond (both normal and fallback paths).  If the guard
    fires it mutates *tool_results* and *tools_called* in place and returns a
    refreshed snapshot; otherwise returns the original snapshot unchanged.
    """
    from app.services.conversation.aria.context_builder import ProjectSnapshot  # noqa: F401

    _conf = next((r for r in tool_results if r.tool_name == ToolName.CONFIRMATION), None)
    if _conf is None or not _conf.data.get("confirmed") or ToolName.RESUMPTION in tools_called:
        return snapshot  # nothing to do

    logger.warning(
        "aria_loop: respond intercepted at step %d — confirmation_agent returned "
        "True but resumption_agent hasn't run; forcing resumption",
        step,
    )
    _res_tool = tool_registry.get(ToolName.RESUMPTION)
    if _res_tool is None:
        return snapshot

    _res_result = _res_tool.execute(db, conversation.project_id, conversation, None)
    tools_called.add(ToolName.RESUMPTION)
    tool_results.append(_res_result)
    db.refresh(conversation)
    return build_snapshot(db, conversation.project_id, conversation)


# ── Phase transitions ─────────────────────────────────────────────────────────


def _apply_phase_transitions(
    db: Session,
    conversation: Conversation,
    aria_step: AriaStep,
    tool_results: list[ToolResult],
) -> None:
    """Update conversation state based on what happened during the loop."""
    now = datetime.now(timezone.utc)

    for result in tool_results:
        data = result.data

        if result.tool_name == ToolName.REQUIREMENTS:
            if data.get("status") == "sufficient":
                conversation.requirements_ready = True
                if data.get("updated_draft"):
                    conversation.requirements_draft = data["updated_draft"]
                if data.get("product_type"):
                    from app.models.project import Project

                    project = db.get(Project, conversation.project_id)
                    if project is not None:
                        project.product_type = data["product_type"]
                        db.add(project)
            elif data.get("updated_draft"):
                conversation.requirements_draft = data["updated_draft"]

        elif result.tool_name == ToolName.START_PROJECT:
            if "error" not in data:
                conversation.phase = CONVERSATION_PHASE_EXECUTING
                conversation.requirements_ready = False

        elif result.tool_name == ToolName.REVIEW:
            status = data.get("status")
            conversation.review_episode_attempts = (conversation.review_episode_attempts or 0) + 1
            if status == "ready_to_confirm":
                conversation.proposed_plan = data.get("clarification_summary") or ""
            elif status == "abandoned":
                _handle_review_abandoned(db, conversation, data)

        elif result.tool_name == ToolName.CONFIRMATION:
            if data.get("confirmed"):
                pass  # resumption_tool will handle the rest; wait for it
            else:
                # User rejected — clear proposed plan, back to gathering clarification
                conversation.proposed_plan = None

        elif result.tool_name == ToolName.RESUMPTION:
            if "error" not in data:
                conversation.proposed_plan = None
                conversation.review_context = None
                conversation.review_episode_attempts = 0
                conversation.phase = CONVERSATION_PHASE_EXECUTING

        elif result.tool_name == ToolName.QA:
            status = data.get("status")
            if status == "qa_started":
                conversation.phase = CONVERSATION_PHASE_QA_RUNNING
                conversation.qa_offer_pending = False
            elif status == "qa_offer_declined":
                conversation.qa_offer_pending = False
            elif status == "remediation_confirmed":
                conversation.pending_qa_report = None
                conversation.phase = CONVERSATION_PHASE_EXECUTING
            elif status == "remediation_declined":
                conversation.pending_qa_report = None

    # Apply transitions from system events
    _apply_system_event_transitions(db, conversation, tool_results)

    conversation.updated_at = now
    db.flush()


def _apply_system_event_transitions(
    db: Session,
    conversation: Conversation,
    tool_results: list[ToolResult],
) -> None:
    """Phase transitions driven directly by system events (no tool results needed)."""
    # These are applied inline when system events arrive, before the loop.
    # This function is a no-op placeholder for any post-loop cleanup.
    pass


def _handle_review_abandoned(
    db: Session,
    conversation: Conversation,
    review_data: dict,
) -> None:
    """Handle the review being abandoned by the user."""
    from app.services.tasks import mark_task_failed

    is_project_level = review_data.get("is_project_level", False)

    blocked_task_id: int | None = None
    blocked_task_title: str | None = None

    if not is_project_level and conversation.review_context:
        try:
            ctx = review_context_from_json(conversation.review_context)
            if isinstance(ctx, TaskReviewContext):
                blocked_task_id = ctx.task_id
                blocked_task_title = ctx.task_title
                mark_task_failed(db, ctx.task_id, auto_commit=False)
        except Exception:
            pass

    _save_abandoned_episode_artifact(
        db=db,
        conversation=conversation,
        is_project_level=is_project_level,
        blocked_task_id=blocked_task_id,
        blocked_task_title=blocked_task_title,
    )

    conversation.proposed_plan = None
    conversation.review_context = None
    conversation.review_episode_attempts = 0

    if is_project_level:
        # Project-level abandonment → pause (infra problem likely persists)
        conversation.phase = CONVERSATION_PHASE_PAUSED
    else:
        conversation.phase = CONVERSATION_PHASE_EXECUTING


def _save_abandoned_episode_artifact(
    db: Session,
    conversation: Conversation,
    *,
    is_project_level: bool,
    blocked_task_id: int | None,
    blocked_task_title: str | None,
) -> None:
    """Persist a review_episode artifact (outcome=abandoned) for the Supervisor."""
    import json

    from app.models.artifact import Artifact

    _ARTIFACT_TYPE = "review_episode"

    try:
        episode_index = (
            db.query(Artifact)
            .filter(
                Artifact.project_id == conversation.project_id,
                Artifact.artifact_type == _ARTIFACT_TYPE,
            )
            .count()
        )
        payload = {
            "episode_index": episode_index,
            "is_project_level": is_project_level,
            "blocked_task_id": blocked_task_id,
            "blocked_task_title": blocked_task_title,
            "review_attempts": conversation.review_episode_attempts or 0,
            "proposed_plan": None,
            "outcome": "abandoned",
            "impact_scope": None,
            "tasks_modified_count": 0,
            "tasks_added_count": 0,
            "tasks_superseded_count": 0,
            "impact_reasoning": None,
        }
        artifact = Artifact(
            project_id=conversation.project_id,
            task_id=blocked_task_id,
            artifact_type=_ARTIFACT_TYPE,
            content=json.dumps(payload),
            created_by="aria_orchestrator",
        )
        db.add(artifact)
        db.flush()
    except Exception:
        pass  # Supervisor instrumentation must never break the agent


# ── System event handling ─────────────────────────────────────────────────────


def apply_system_event_pre_loop(
    db: Session,
    conversation: Conversation,
    aria_input: AriaInput,
) -> None:
    """Apply DB state changes that must happen before the Aria LLM loop runs."""
    if aria_input.source != "system" or aria_input.system_event is None:
        return

    event = aria_input.system_event
    now = datetime.now(timezone.utc)

    if event.event_type == SystemEventType.EXECUTION_STARTED:
        conversation.phase = CONVERSATION_PHASE_EXECUTING
        conversation.updated_at = now
        db.flush()

    elif event.event_type == SystemEventType.MANUAL_REVIEW_REQUIRED:
        task_id = event.data.get("task_id")
        task_title = event.data.get("task_title", "Tarea desconocida")
        task_description = event.data.get("task_description")
        validation_notes = event.data.get("validation_notes")

        ctx = TaskReviewContext(
            task_id=task_id or 0,
            task_title=task_title,
            task_description=task_description,
            validation_notes=validation_notes,
        )
        conversation.phase = CONVERSATION_PHASE_REVIEWING
        conversation.review_context = review_context_to_json(ctx)
        conversation.review_episode_attempts = 0
        conversation.proposed_plan = None
        conversation.updated_at = now
        db.flush()

    elif event.event_type == SystemEventType.WORKFLOW_ERROR:
        failure_reason = event.data.get("failure_reason", "Error desconocido")
        failure_type = classify_workflow_failure(failure_reason)

        ctx = ProjectReviewContext(
            failure_type=failure_type,
            failure_reason=failure_reason,
        )
        conversation.phase = CONVERSATION_PHASE_REVIEWING
        conversation.review_context = review_context_to_json(ctx)
        conversation.review_episode_attempts = 0
        conversation.proposed_plan = None
        conversation.updated_at = now
        db.flush()

    elif event.event_type == SystemEventType.PROJECT_COMPLETED:
        conversation.phase = CONVERSATION_PHASE_COMPLETED
        # Offer QA if the product type is eligible
        from app.models.project import Project
        from app.services.qa.strategy_selector import QA_ELIGIBLE_PRODUCT_TYPES

        project = db.get(Project, conversation.project_id)
        if project and project.product_type in QA_ELIGIBLE_PRODUCT_TYPES:
            conversation.qa_offer_pending = True
        conversation.updated_at = now
        db.flush()

    elif event.event_type == SystemEventType.QA_COMPLETED:
        if conversation.phase == CONVERSATION_PHASE_QA_RUNNING:
            conversation.phase = CONVERSATION_PHASE_COMPLETED
        # pending_qa_report may already be set by qa_runner before calling
        # process_with_pre_transitions; only write it if it is still absent.
        if not conversation.pending_qa_report and event.data:
            conversation.pending_qa_report = json.dumps(event.data)
        conversation.updated_at = now
        db.flush()


def process_with_pre_transitions(
    db: Session,
    conversation_id: int,
    aria_input: AriaInput,
) -> AriaResponse:
    """process() variant that applies system event pre-transitions before the loop.

    This is the function the router should call for system events.
    For user messages, the regular process() is identical.
    """
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise AriaOrchestratorError(f"Conversation {conversation_id} not found")

    db.refresh(conversation)

    if conversation.status != CONVERSATION_STATUS_ACTIVE:
        raise AriaOrchestratorError(
            f"Conversation {conversation_id} is not active (status={conversation.status})"
        )

    # Apply state transitions driven by the system event before building snapshot
    apply_system_event_pre_loop(db, conversation, aria_input)

    snapshot = build_snapshot(db, conversation.project_id, conversation)

    # Persist the incoming input as a message
    if aria_input.source == "user" and aria_input.user_message:
        _append_message(db, conversation.id, MESSAGE_ROLE_USER, aria_input.user_message)
    elif aria_input.source == "system" and aria_input.system_event:
        system_text = _format_system_event(aria_input)
        _append_message(db, conversation.id, MESSAGE_ROLE_SYSTEM, system_text)

    return _run_loop(db, conversation, snapshot, aria_input)


# ── LLM call ─────────────────────────────────────────────────────────────────


def _call_aria_llm(
    snapshot: ProjectSnapshot,
    conversation: Conversation,
    aria_input: AriaInput,
    tool_results: list[ToolResult],
    *,
    force_respond: bool = False,
) -> AriaStep:
    """Call the Aria LLM and return a structured AriaStep decision."""
    provider = get_llm_provider()

    user_prompt = _build_user_prompt(
        snapshot, conversation, aria_input, tool_results, force_respond
    )

    schema = AriaStep.model_json_schema()
    strict_schema = to_openai_strict_json_schema(schema)

    raw = provider.generate_structured(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="aria_step",
        json_schema=strict_schema,
    )

    step = AriaStep.model_validate(raw)

    # Safety: if force_respond was requested, override any call_tool action
    if force_respond and step.action == "call_tool":
        step = AriaStep(
            action="respond",
            response=step.response
            or "Lo siento, no pude procesar tu mensaje correctamente. ¿Puedes intentarlo de nuevo?",
            reasoning=step.reasoning,
        )

    return step


def _build_user_prompt(
    snapshot: ProjectSnapshot,
    conversation: Conversation,
    aria_input: AriaInput,
    tool_results: list[ToolResult],
    force_respond: bool,
) -> str:
    from app.services.prompt_loader import prompt_loader

    prompt_loader.validate_builder_inputs(
        "aria_orchestrator",
        "main",
        {
            "project_state": snapshot,
            "conversation_history": conversation,
            "current_input": aria_input,
            "tool_results": tool_results,
            "force_respond": force_respond,
        },
    )
    parts: list[str] = []

    # Project context snapshot
    parts.append("## Estado del proyecto\n" + snapshot.format_for_prompt())

    # Recent conversation history
    history = _load_recent_history(conversation)
    if history:
        parts.append("## Historial reciente")
        for msg in history[-_HISTORY_LIMIT:]:
            role_label = {"user": "Usuario", "assistant": "Aria", "system": "Sistema"}.get(
                msg["role"], msg["role"]
            )
            parts.append(f"**{role_label}:** {msg['content']}")

    # Current input
    parts.append("## Input actual")
    if aria_input.source == "user":
        parts.append(f"**Usuario:** {aria_input.user_message or ''}")
    else:
        parts.append(f"**Evento de sistema:** {_format_system_event(aria_input)}")

    # Tool results from previous iterations in this turn
    if tool_results:
        parts.append("## Resultados de herramientas (este turno)")
        for tr in tool_results:
            parts.append(f"**{tr.tool_name.value}:** {json.dumps(tr.data, ensure_ascii=False)}")
            if tr.side_effects_summary:
                parts.append(f"  Efectos: {tr.side_effects_summary}")

    # Force respond instruction
    if force_respond:
        parts.append(
            "\n⚠️ Debes responder directamente ahora (action='respond'). "
            "No puedes llamar más herramientas."
        )

    return "\n\n".join(parts)


def _load_recent_history(conversation: Conversation) -> list[dict[str, str]]:
    """Return the in-memory messages from the conversation relationship (already loaded)."""
    if not conversation.messages:
        return []
    recent = list(conversation.messages)[-_HISTORY_LIMIT:]
    return [{"role": m.role, "content": m.content} for m in recent]


# ── Response finalisation ─────────────────────────────────────────────────────


def _finalise_response(
    db: Session,
    conversation: Conversation,
    snapshot: ProjectSnapshot,
    aria_step: AriaStep,
    tool_results: list[ToolResult],
    aria_input: AriaInput | None = None,
    *,
    is_fallback: bool = False,
) -> AriaResponse:
    response_text = aria_step.response or "¿Puedes repetir eso? No he podido procesar tu mensaje."

    # Apply phase transitions before persisting the response
    _apply_phase_transitions(db, conversation, aria_step, tool_results)

    # Persist Aria's response
    _append_message(db, conversation.id, MESSAGE_ROLE_ASSISTANT, response_text)
    db.commit()
    db.refresh(conversation)

    event = _determine_event(tool_results, is_fallback, conversation, aria_input)

    return AriaResponse(
        message=response_text,
        phase=conversation.phase,
        conversation_id=conversation.id,
        event=event,
        project_id=conversation.project_id,
        requirements_draft=conversation.requirements_draft,
        requirements_ready=conversation.requirements_ready,
    )


def _determine_event(
    tool_results: list[ToolResult],
    is_fallback: bool,
    conversation: Conversation | None = None,
    aria_input: AriaInput | None = None,
) -> str:
    if is_fallback:
        # Even on the fallback path, if the resumption guard forced execution we must
        # surface "review_resolved" so the router can restart the workflow.
        _res = next((r for r in tool_results if r.tool_name == ToolName.RESUMPTION), None)
        if _res is not None and "error" not in _res.data:
            return "review_resolved"
        return "fallback"

    # ── Tool results take priority when present ───────────────────────────────
    for result in reversed(tool_results):
        data = result.data
        if result.tool_name == ToolName.QA:
            status = data.get("status")
            if status == "qa_started":
                return "qa_running"
            if status == "qa_offer":
                return "qa_offer"
            if status == "qa_offer_declined":
                return "project_completed"
            if status == "qa_completed_no_issues":
                return "qa_completed_no_issues"
            if status == "qa_completed_with_findings":
                return "qa_completed_with_findings"
            if status == "remediation_confirmed":
                return "qa_remediation_started"
            if status == "remediation_declined":
                return "qa_completed_no_issues"
        if result.tool_name == ToolName.START_PROJECT and "error" not in data:
            return "project_started"
        if result.tool_name == ToolName.RESUMPTION and "error" not in data:
            return "review_resolved"
        if result.tool_name == ToolName.REVIEW:
            status = data.get("status")
            if status == "ready_to_confirm":
                return "review_confirmation_requested"
            if status == "abandoned":
                return "review_closed_abandoned"
        if result.tool_name == ToolName.REQUIREMENTS:
            if data.get("status") == "sufficient":
                return "requirements_ready"
        if result.tool_name == ToolName.QUERY:
            return "project_query_answered"

    # ── No tool results — derive from phase or system event ───────────────────
    if conversation is not None:
        if conversation.phase == CONVERSATION_PHASE_COMPLETED:
            # After QA_COMPLETED: pending report was stored
            if conversation.pending_qa_report:
                return "qa_completed_with_findings"
            # After PROJECT_COMPLETED for eligible product: QA offer
            if conversation.qa_offer_pending:
                return "qa_offer"
            return "project_completed"
        if conversation.phase == CONVERSATION_PHASE_REVIEWING:
            return "review_started"

    if aria_input is not None and aria_input.source == "system" and aria_input.system_event:
        evt = aria_input.system_event.event_type
        if evt == SystemEventType.EXECUTION_STARTED:
            return "execution_started"
        if evt == SystemEventType.PROJECT_COMPLETED:
            return "project_completed"
        if evt in (SystemEventType.MANUAL_REVIEW_REQUIRED, SystemEventType.WORKFLOW_ERROR):
            return "review_started"

    return "question"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_tool_registry() -> dict[
    ToolName,
    RequirementsTool
    | QueryTool
    | ReviewTool
    | ConfirmationTool
    | StartProjectTool
    | ResumptionTool
    | QATool,
]:
    return {
        ToolName.REQUIREMENTS: RequirementsTool(),
        ToolName.QUERY: QueryTool(),
        ToolName.REVIEW: ReviewTool(),
        ToolName.CONFIRMATION: ConfirmationTool(),
        ToolName.START_PROJECT: StartProjectTool(),
        ToolName.RESUMPTION: ResumptionTool(),
        ToolName.QA: QATool(),
    }


def _append_message(
    db: Session,
    conversation_id: int,
    role: str,
    content: str,
) -> ConversationMessage:
    msg = ConversationMessage(
        conversation_id=conversation_id,
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.flush()
    return msg


def _format_system_event(aria_input: AriaInput) -> str:
    if aria_input.system_event is None:
        return "[system event]"
    event = aria_input.system_event
    data_str = json.dumps(event.data, ensure_ascii=False) if event.data else ""
    return f"[{event.event_type.value}] {data_str}".strip()


# ── Project completion check ──────────────────────────────────────────────────


def check_project_completed(db: Session, project_id: int) -> AriaResponse | None:
    """Called after each task event. If all atomic tasks are terminal, notify Aria."""
    from app.models.task import TASK_STATUS_COMPLETED, TASK_STATUS_FAILED

    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.project_id == project_id,
            Conversation.status == CONVERSATION_STATUS_ACTIVE,
        )
        .first()
    )
    if conversation is None or conversation.phase != CONVERSATION_PHASE_EXECUTING:
        return None

    atomic_tasks: list[Task] = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.planning_level == PLANNING_LEVEL_ATOMIC,
        )
        .all()
    )
    if not atomic_tasks:
        return None
    if not all(t.status in TERMINAL_TASK_STATUSES for t in atomic_tasks):
        return None

    completed_count = sum(1 for t in atomic_tasks if t.status == TASK_STATUS_COMPLETED)
    failed_count = sum(1 for t in atomic_tasks if t.status == TASK_STATUS_FAILED)

    from app.services.conversation.aria.contracts import SystemEvent

    system_event = SystemEvent(
        event_type=SystemEventType.PROJECT_COMPLETED,
        data={"completed": completed_count, "failed": failed_count},
    )
    aria_input = AriaInput(source="system", system_event=system_event)

    return process_with_pre_transitions(db, conversation.id, aria_input)
