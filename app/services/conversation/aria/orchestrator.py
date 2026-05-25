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
    QueryTool,
    RequirementsTool,
    ResumptionTool,
    ReviewTool,
    StartProjectTool,
)
from app.services.llm.factory import get_llm_provider
from app.services.llm.schema_utils import to_openai_strict_json_schema

logger = logging.getLogger(__name__)

MAX_STEPS = 4
_HISTORY_LIMIT = 40  # messages sent to Aria's prompt


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Eres Aria, el asistente conversacional de una plataforma de desarrollo de software autónomo.
Tu rol es orquestar la conversación entre el usuario y el sistema de desarrollo.

## Tus herramientas

- **requirements_agent**: Evalúa si los requisitos del proyecto son suficientes para empezar.
  Úsala cuando el usuario está describiendo lo que quiere construir (fase: gathering_requirements).

- **query_agent**: Responde preguntas sobre el estado actual del proyecto (tareas, progreso, errores).
  Disponible en CUALQUIER fase — el usuario siempre puede pedir información.

- **review_agent**: Evalúa las aclaraciones del usuario cuando hay una tarea o error bloqueado.
  Úsala cuando fase=reviewing y el usuario está aportando información para resolver el bloqueo.

- **confirmation_agent**: Interpreta si el usuario confirma o rechaza un plan propuesto.
  Úsala cuando proposed_plan tiene valor y el usuario acaba de responder al plan.

- **start_project**: Inicia el proyecto creando el plan de tareas atómicas.
  Úsala cuando requirements_ready=true y el usuario confirma que quiere empezar,
  o cuando llega el evento de sistema confirm_start.

- **resumption_agent**: Aplica la aclaración confirmada al grafo de tareas y reanuda la ejecución.
  Úsala inmediatamente después de que confirmation_agent devuelva confirmed=true.

## Restricciones

- No puedes llamar a la misma herramienta más de una vez por turno.
- Si no entiendes qué quiere el usuario, responde directamente sin llamar herramientas (fallback).
- Las herramientas devuelven datos estructurados; tú sintetizas la respuesta final al usuario.

## Hints de fase

- **gathering_requirements**: Foco en requirements_agent y start_project.
- **executing**: Foco en query_agent. No modifiques el plan.
- **reviewing**: Foco en review_agent, confirmation_agent y resumption_agent.
  El usuario también puede preguntar el estado — usa query_agent si lo pide.
- **paused**: Foco en query_agent.
- **completed**: Foco en query_agent.

## Eventos de sistema

Cuando source=system recibirás eventos del workflow. Responde al usuario en consecuencia:
- execution_started: El proyecto ha comenzado a ejecutarse.
- manual_review_required: Una tarea necesita la atención del usuario.
- workflow_error: El flujo se ha detenido por un error de proyecto.
- project_completed: El proyecto ha finalizado.
- confirm_start: El usuario ha confirmado el inicio desde el botón — usa start_project.

## Idioma

Responde SIEMPRE en el mismo idioma que el último mensaje del usuario.
Si el input es un evento de sistema, responde en el idioma de la conversación previa.
"""


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

    # Exhausted steps or broke out — force a respond call
    logger.warning("aria_loop: exhausted steps or broke early — forcing fallback respond")
    aria_step = _call_aria_llm(snapshot, conversation, aria_input, tool_results, force_respond=True)
    return _finalise_response(
        db, conversation, snapshot, aria_step, tool_results, aria_input, is_fallback=True
    )


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

    if not is_project_level and conversation.review_context:
        try:
            ctx = review_context_from_json(conversation.review_context)
            if isinstance(ctx, TaskReviewContext):
                mark_task_failed(db, ctx.task_id, auto_commit=False)
        except Exception:
            pass

    conversation.proposed_plan = None
    conversation.review_context = None
    conversation.review_episode_attempts = 0

    if is_project_level:
        # Project-level abandonment → pause (infra problem likely persists)
        conversation.phase = CONVERSATION_PHASE_PAUSED
    else:
        conversation.phase = CONVERSATION_PHASE_EXECUTING


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
        return "fallback"

    # Events derived from final conversation phase (system event transitions)
    if conversation is not None:
        if conversation.phase == CONVERSATION_PHASE_COMPLETED:
            return "project_completed"
        if conversation.phase == CONVERSATION_PHASE_REVIEWING and not tool_results:
            return "review_started"

    # Events derived from system event type directly (when no tool results)
    if aria_input is not None and aria_input.source == "system" and aria_input.system_event:
        evt = aria_input.system_event.event_type
        if evt == SystemEventType.EXECUTION_STARTED:
            return "execution_started"
        if evt == SystemEventType.PROJECT_COMPLETED:
            return "project_completed"
        if evt in (SystemEventType.MANUAL_REVIEW_REQUIRED, SystemEventType.WORKFLOW_ERROR):
            return "review_started"

    # Derive event from the most significant tool result
    for result in reversed(tool_results):
        data = result.data
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

    return "question"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_tool_registry() -> dict[
    ToolName,
    RequirementsTool
    | QueryTool
    | ReviewTool
    | ConfirmationTool
    | StartProjectTool
    | ResumptionTool,
]:
    return {
        ToolName.REQUIREMENTS: RequirementsTool(),
        ToolName.QUERY: QueryTool(),
        ToolName.REVIEW: ReviewTool(),
        ToolName.CONFIRMATION: ConfirmationTool(),
        ToolName.START_PROJECT: StartProjectTool(),
        ToolName.RESUMPTION: ResumptionTool(),
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
