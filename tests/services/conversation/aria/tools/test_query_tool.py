"""Tests for QueryTool — including error summary enrichment for failed tasks."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models.conversation import (
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.execution_run import ExecutionRun
from app.models.task import (
    PLANNING_LEVEL_ATOMIC,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    Task,
)
from app.services.conversation.aria.contracts import ToolName
from app.services.conversation.aria.tools.query_tool import QueryTool

_MODULE = "app.services.conversation.aria.tools.query_tool"


def _make_conversation(db: Session, project_id: int) -> Conversation:
    conv = Conversation(
        project_id=project_id,
        status=CONVERSATION_STATUS_ACTIVE,
        phase=CONVERSATION_PHASE_EXECUTING,
        review_episode_attempts=0,
        requirements_ready=False,
    )
    db.add(conv)
    db.flush()
    return conv


def _make_task(
    db: Session,
    project_id: int,
    status: str,
    title: str = "A task",
) -> Task:
    task = Task(
        project_id=project_id,
        title=title,
        description="desc",
        planning_level=PLANNING_LEVEL_ATOMIC,
        status=status,
    )
    db.add(task)
    db.flush()
    return task


def _make_execution_run(
    db: Session,
    task_id: int,
    *,
    validation_notes: str | None = None,
    blockers_found: str | None = None,
    error_message: str | None = None,
) -> ExecutionRun:
    run = ExecutionRun(
        task_id=task_id,
        agent_name="execution_engine",
        attempt_number=1,
        status="failed",
        validation_notes=validation_notes,
        blockers_found=blockers_found,
        error_message=error_message,
    )
    db.add(run)
    db.flush()
    return run


class TestQueryToolErrorSummaries:
    def test_error_summary_included_for_failed_task(self, db_session: Session, make_project):
        """Failed tasks with ExecutionRun data get error_summary in the query input."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        task = _make_task(db_session, project.id, TASK_STATUS_FAILED, "Setup database")
        _make_execution_run(
            db_session,
            task.id,
            validation_notes="Connection refused on port 5432",
            blockers_found="PostgreSQL not running",
            error_message=None,
        )
        db_session.commit()

        captured: dict = {}

        def _mock_answer(db, *, error_summaries=None, **kwargs):
            captured["error_summaries"] = error_summaries
            return "The database task failed due to connection issues."

        with patch(
            f"{_MODULE}.answer_project_query",
            side_effect=_mock_answer,
        ):
            QueryTool().execute(db_session, project.id, conv, hint="Why did Setup database fail?")

        assert captured["error_summaries"] is not None
        assert task.id in captured["error_summaries"]
        summary = captured["error_summaries"][task.id]
        assert "Connection refused on port 5432" in summary
        assert "PostgreSQL not running" in summary

    def test_error_summary_included_for_partial_task(self, db_session: Session, make_project):
        """Partial (partial) tasks also get error_summary populated."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        task = _make_task(db_session, project.id, TASK_STATUS_PARTIAL, "Write tests")
        _make_execution_run(
            db_session,
            task.id,
            error_message="Test runner exited with code 1",
        )
        db_session.commit()

        captured: dict = {}

        def _mock_answer(db, *, error_summaries=None, **kwargs):
            captured["error_summaries"] = error_summaries
            return "partial info"

        with patch(f"{_MODULE}.answer_project_query", side_effect=_mock_answer):
            QueryTool().execute(db_session, project.id, conv)

        assert task.id in (captured.get("error_summaries") or {})

    def test_completed_tasks_have_no_error_summary(self, db_session: Session, make_project):
        """Completed tasks are never queried for error summaries."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        task = _make_task(db_session, project.id, TASK_STATUS_COMPLETED, "Setup API")
        _make_execution_run(db_session, task.id, error_message="some old error")
        db_session.commit()

        captured: dict = {}

        def _mock_answer(db, *, error_summaries=None, **kwargs):
            captured["error_summaries"] = error_summaries
            return "all done"

        with patch(f"{_MODULE}.answer_project_query", side_effect=_mock_answer):
            QueryTool().execute(db_session, project.id, conv)

        assert task.id not in (captured.get("error_summaries") or {})

    def test_failed_task_without_execution_run_has_no_summary(
        self, db_session: Session, make_project
    ):
        """Failed task with no ExecutionRun is omitted from error_summaries dict."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        task = _make_task(db_session, project.id, TASK_STATUS_FAILED, "No run task")
        # No ExecutionRun created
        db_session.commit()

        captured: dict = {}

        def _mock_answer(db, *, error_summaries=None, **kwargs):
            captured["error_summaries"] = error_summaries
            return "nothing"

        with patch(f"{_MODULE}.answer_project_query", side_effect=_mock_answer):
            QueryTool().execute(db_session, project.id, conv)

        assert task.id not in (captured.get("error_summaries") or {})

    def test_error_summary_combines_all_three_fields(self, db_session: Session, make_project):
        """error_summary = validation_notes | blockers_found | error_message."""
        project = make_project()
        conv = _make_conversation(db_session, project.id)
        task = _make_task(db_session, project.id, TASK_STATUS_FAILED, "Deploy")
        _make_execution_run(
            db_session,
            task.id,
            validation_notes="Tests failed: 3 errors",
            blockers_found="Missing env var DATABASE_URL",
            error_message="Exit code 1",
        )
        db_session.commit()

        captured: dict = {}

        def _mock_answer(db, *, error_summaries=None, **kwargs):
            captured["error_summaries"] = error_summaries
            return "info"

        with patch(f"{_MODULE}.answer_project_query", side_effect=_mock_answer):
            QueryTool().execute(db_session, project.id, conv)

        summary = captured["error_summaries"][task.id]
        assert "Tests failed: 3 errors" in summary
        assert "Missing env var DATABASE_URL" in summary
        assert "Exit code 1" in summary

    def test_tool_name(self):
        assert QueryTool().name == ToolName.QUERY


class TestRenderTaskListWithError:
    def test_error_summary_appears_in_failed_task_rendering(self):
        from app.services.conversation.project_query_agent import _render_task_list

        tasks = [
            {"id": 1, "title": "Setup DB", "error_summary": "Connection refused on port 5432"},
            {"id": 2, "title": "Run migrations"},
        ]
        rendered = _render_task_list(tasks, "FAILED TASKS")
        assert "Setup DB" in rendered
        assert "Connection refused on port 5432" in rendered
        assert "[Error:" in rendered
        # Task without error_summary has no error line
        assert "Run migrations" in rendered

    def test_long_error_summary_truncated_to_300_chars(self):
        from app.services.conversation.project_query_agent import _render_task_list

        tasks = [{"id": 1, "title": "T", "error_summary": "X" * 400}]
        rendered = _render_task_list(tasks, "FAILED")
        assert "X" * 300 in rendered
        assert "…" in rendered
        assert "X" * 301 not in rendered
