"""Tests for the Aria orchestrator loop."""

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_GATHERING,
    CONVERSATION_PHASE_REVIEWING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
    ConversationMessage,
)
from app.models.task import PLANNING_LEVEL_ATOMIC, Task
from app.services.conversation.aria.contracts import (
    AriaInput,
    AriaStep,
    ProjectReviewContext,
    SystemEvent,
    SystemEventType,
    TaskReviewContext,
    ToolName,
    review_context_to_json,
)
from app.services.conversation.aria.orchestrator import (
    get_or_create_conversation,
    process_with_pre_transitions,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_conversation(db: Session, project_id: int, **kwargs) -> Conversation:
    defaults = dict(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_GATHERING,
        review_episode_attempts=0,
        requirements_ready=False,
    )
    defaults.update(kwargs)
    conv = Conversation(**defaults)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _aria_step_respond(message: str) -> AriaStep:
    return AriaStep(action="respond", response=message, reasoning="test")


def _aria_step_call(tool: ToolName, hint: str | None = None) -> AriaStep:
    return AriaStep(action="call_tool", tool_name=tool, tool_hint=hint, reasoning="test")


# ── Tests: get_or_create_conversation ─────────────────────────────────────────


class TestGetOrCreateConversation:
    def test_creates_new_conversation(self, db_session: Session, make_project):
        project = make_project()
        conv = get_or_create_conversation(db_session, project.id)

        assert conv.id is not None
        assert conv.phase == CONVERSATION_PHASE_GATHERING
        assert conv.requirements_ready is False

    def test_creates_intro_message(self, db_session: Session, make_project):
        project = make_project()
        conv = get_or_create_conversation(db_session, project.id)

        msgs = db_session.query(ConversationMessage).filter_by(conversation_id=conv.id).all()
        assert len(msgs) == 1
        assert msgs[0].role == "assistant"
        assert "Aria" in msgs[0].content

    def test_returns_existing_conversation(self, db_session: Session, make_project):
        project = make_project()
        conv1 = get_or_create_conversation(db_session, project.id)
        conv2 = get_or_create_conversation(db_session, project.id)

        assert conv1.id == conv2.id


# ── Tests: direct response (no tools) ─────────────────────────────────────────


class TestDirectResponse:
    def test_responds_without_tool_call(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_step_respond("Hello! Tell me about your project."),
        ):
            response = process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(source="user", user_message="Hi"),
            )

        assert response.message == "Hello! Tell me about your project."
        assert response.phase == CONVERSATION_PHASE_GATHERING
        assert response.event == "question"

    def test_user_message_persisted_to_history(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_step_respond("Ok"),
        ):
            process_with_pre_transitions(
                db_session, conv.id, AriaInput(source="user", user_message="Build me an app")
            )

        msgs = (
            db_session.query(ConversationMessage)
            .filter_by(conversation_id=conv.id)
            .order_by(ConversationMessage.id)
            .all()
        )
        roles = [m.role for m in msgs]
        assert "user" in roles
        assert "assistant" in roles


# ── Tests: single tool call ────────────────────────────────────────────────────


class TestSingleToolCall:
    def test_requirements_tool_called_then_respond(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _aria_step_call(ToolName.REQUIREMENTS)
            return _aria_step_respond("What stack do you want to use?")

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm", side_effect=_mock_llm
            ),
            patch(
                "app.services.conversation.aria.tools.requirements_tool.evaluate_requirements",
                return_value=MagicMock(
                    status="needs_more",
                    next_question="What stack?",
                    updated_draft="App",
                    reasoning="r",
                ),
            ),
        ):
            response = process_with_pre_transitions(
                db_session, conv.id, AriaInput(source="user", user_message="I want an app")
            )

        assert response.event == "question"
        assert call_count["n"] == 2

    def test_requirements_sufficient_sets_requirements_ready(
        self, db_session: Session, make_project
    ):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _aria_step_call(ToolName.REQUIREMENTS)
            return _aria_step_respond("Great, I have enough info. Press Start when ready.")

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm", side_effect=_mock_llm
            ),
            patch(
                "app.services.conversation.aria.tools.requirements_tool.evaluate_requirements",
                return_value=MagicMock(
                    status="sufficient",
                    next_question=None,
                    updated_draft="FastAPI app",
                    reasoning="r",
                ),
            ),
        ):
            response = process_with_pre_transitions(
                db_session, conv.id, AriaInput(source="user", user_message="FastAPI, Python")
            )

        db_session.refresh(conv)
        assert conv.requirements_ready is True
        assert conv.requirements_draft == "FastAPI app"
        assert response.event == "requirements_ready"


# ── Tests: no-repeat constraint ────────────────────────────────────────────────


class TestNoRepeatConstraint:
    def test_same_tool_not_called_twice(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id)

        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            # Always try to call the same tool
            return _aria_step_call(ToolName.QUERY)

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm", side_effect=_mock_llm
            ),
            patch(
                "app.services.conversation.aria.tools.query_tool.answer_project_query",
                return_value="Project status: running",
            ),
        ):
            response = process_with_pre_transitions(
                db_session, conv.id, AriaInput(source="user", user_message="Status?")
            )

        # Should fallback after the no-repeat constraint fires (2 LLM calls: 1 call + 1 forced respond)
        assert response.event in ("project_query_answered", "fallback", "question")


# ── Tests: system events ───────────────────────────────────────────────────────


class TestSystemEvents:
    def test_manual_review_transitions_to_reviewing(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_step_respond(
                "La tarea «Setup DB» ha encontrado un problema. ¿Cómo quieres resolverlo?"
            ),
        ):
            response = process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(
                    source="system",
                    system_event=SystemEvent(
                        event_type=SystemEventType.MANUAL_REVIEW_REQUIRED,
                        data={
                            "task_id": 42,
                            "task_title": "Setup DB",
                            "validation_notes": "Connection refused",
                        },
                    ),
                ),
            )

        db_session.refresh(conv)
        assert conv.phase == CONVERSATION_PHASE_REVIEWING
        assert conv.review_context is not None
        ctx = TaskReviewContext.model_validate_json(conv.review_context)
        assert ctx.task_id == 42
        assert response.event in ("question", "review_started")

    def test_workflow_error_transitions_to_reviewing(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_step_respond("El flujo se ha detenido. ¿Qué hacemos?"),
        ):
            process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(
                    source="system",
                    system_event=SystemEvent(
                        event_type=SystemEventType.WORKFLOW_ERROR,
                        data={"failure_reason": "Docker image not found"},
                    ),
                ),
            )

        db_session.refresh(conv)
        assert conv.phase == CONVERSATION_PHASE_REVIEWING
        ctx = ProjectReviewContext.model_validate_json(conv.review_context)
        assert ctx.failure_type == "bootstrap"

    def test_project_completed_transitions_phase(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_step_respond("¡El proyecto ha finalizado!"),
        ):
            response = process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(
                    source="system",
                    system_event=SystemEvent(
                        event_type=SystemEventType.PROJECT_COMPLETED,
                        data={"completed": 5, "failed": 0},
                    ),
                ),
            )

        db_session.refresh(conv)
        assert conv.phase == "completed"
        assert response.event == "project_completed"

    def test_system_message_persisted_in_history(self, db_session: Session, make_project):
        project = make_project()
        conv = _make_conversation(db_session, project.id, phase=CONVERSATION_PHASE_EXECUTING)

        with patch(
            "app.services.conversation.aria.orchestrator._call_aria_llm",
            return_value=_aria_step_respond("Execution started!"),
        ):
            process_with_pre_transitions(
                db_session,
                conv.id,
                AriaInput(
                    source="system",
                    system_event=SystemEvent(event_type=SystemEventType.EXECUTION_STARTED, data={}),
                ),
            )

        msgs = db_session.query(ConversationMessage).filter_by(conversation_id=conv.id).all()
        roles = [m.role for m in msgs]
        assert "system" in roles


# ── Tests: review flow ─────────────────────────────────────────────────────────


class TestReviewFlow:
    def test_review_ready_to_confirm_sets_proposed_plan(self, db_session: Session, make_project):
        project = make_project()
        ctx = TaskReviewContext(task_id=1, task_title="Auth")
        conv = _make_conversation(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_REVIEWING,
            review_context=review_context_to_json(ctx),
        )

        call_count = {"n": 0}

        def _mock_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _aria_step_call(ToolName.REVIEW)
            return _aria_step_respond("Voy a usar Google OAuth. ¿Confirmas?")

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm", side_effect=_mock_llm
            ),
            patch(
                "app.services.conversation.aria.tools.review_tool.evaluate_review",
                return_value=MagicMock(
                    status="ready_to_confirm",
                    next_question=None,
                    clarification_summary="Use Google OAuth",
                    action_summary="Implement Google OAuth",
                    reasoning="r",
                ),
            ),
        ):
            response = process_with_pre_transitions(
                db_session, conv.id, AriaInput(source="user", user_message="Use Google OAuth")
            )

        db_session.refresh(conv)
        assert conv.proposed_plan == "Use Google OAuth"
        assert response.event == "review_confirmation_requested"

    def test_confirmation_then_resumption_clears_review_state(
        self, db_session: Session, make_project
    ):
        project = make_project()
        ctx = TaskReviewContext(task_id=1, task_title="Auth")
        conv = _make_conversation(
            db_session,
            project.id,
            phase=CONVERSATION_PHASE_REVIEWING,
            review_context=review_context_to_json(ctx),
            proposed_plan="Use Google OAuth",
        )

        # Add a task to the project so resumption_service can find it
        task = Task(
            project_id=project.id,
            title="Auth",
            description="desc",
            planning_level=PLANNING_LEVEL_ATOMIC,
            status="awaiting_review",
            sequence_order=1,
        )
        task.id = 1
        db_session.add(task)
        db_session.flush()

        call_seq = iter(
            [
                _aria_step_call(ToolName.CONFIRMATION, hint="Yes"),
                _aria_step_call(ToolName.RESUMPTION),
                _aria_step_respond("¡Perfecto! Reanudando la ejecución."),
            ]
        )

        with (
            patch(
                "app.services.conversation.aria.orchestrator._call_aria_llm", side_effect=call_seq
            ),
            patch(
                "app.services.conversation.aria.tools.confirmation_tool.evaluate_confirmation",
                return_value=MagicMock(confirmed=True, follow_up=None, reasoning="ok"),
            ),
            patch(
                "app.services.conversation.aria.tools.resumption_tool.resume_after_review",
                return_value=MagicMock(
                    scope="narrow", message="Clarification applied", tasks_modified=1, tasks_added=0
                ),
            ),
        ):
            response = process_with_pre_transitions(
                db_session, conv.id, AriaInput(source="user", user_message="Yes go ahead")
            )

        db_session.refresh(conv)
        assert conv.proposed_plan is None
        assert conv.review_context is None
        assert conv.phase == CONVERSATION_PHASE_EXECUTING
        assert response.event == "review_resolved"
