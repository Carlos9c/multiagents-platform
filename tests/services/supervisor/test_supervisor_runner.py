"""
Tests for the supervisor runner.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.agent_evaluation import AgentEvaluation
from app.models.conversation import Conversation
from app.models.supervisor_report import (
    SUPERVISOR_REPORT_STATUS_COMPLETED,
    SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED,
    SupervisorReport,
)
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.supervisor_runner import _overall_verdict, run_supervisor

_MODULE = "app.services.supervisor.supervisor_runner"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEALTHY_OUTPUT = EvaluatorOutput(
    result=AgentEvaluationOutput(
        verdict="healthy",
        findings="Agent performed well across all tasks.",
        issues=[],
        suggestions=[],
    )
)

_DEGRADED_OUTPUT = EvaluatorOutput(
    result=AgentEvaluationOutput(
        verdict="degraded",
        findings="Systematic failures detected in this agent's outputs.",
        issues=["Repeated failure pattern"],
        suggestions=["Fix root cause"],
    )
)

_NOT_SUPERVISED_OUTPUT = EvaluatorOutput(result=None)

_SYNTHESIS_TEXT = (
    "## Overall Verdict\n\nThe system is healthy overall.\n\n"
    "## Healthy Agents\n\nAll agents performed well."
)


def _make_evaluator_mock(name: str, output: EvaluatorOutput) -> MagicMock:
    mock = MagicMock()
    mock.AGENT_NAME = name
    mock.evaluate.return_value = output
    return mock


# ---------------------------------------------------------------------------
# _overall_verdict unit tests
# ---------------------------------------------------------------------------


def test_overall_verdict_all_healthy():
    assert _overall_verdict(["healthy", "healthy", "healthy"]) == "healthy"


def test_overall_verdict_none_below_threshold_excluded_from_average():
    # 1 not_supervised out of 5 = 20% ≤ 30% → excluded from score only
    # scores: [2, 0, 2, 2] / 4 = 1.5 → needs_attention
    assert (
        _overall_verdict(["healthy", "degraded", "healthy", "healthy", None]) == "needs_attention"
    )


def test_overall_verdict_all_none_returns_not_evaluated():
    # 100% not_supervised > 30% → not_evaluated
    assert _overall_verdict([None, None]) == SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED


def test_overall_verdict_empty_defaults_healthy():
    assert _overall_verdict([]) == "healthy"


# Score-based thresholds: healthy>=1.7, needs_attention>=1.3, degraded<1.3
# With 22 agents all healthy except the degraded ones listed below:


def test_overall_verdict_one_degraded_out_of_three_is_degraded():
    # (2+0+1)/3 = 1.0 < 1.3 → degraded
    assert _overall_verdict(["healthy", "degraded", "needs_attention"]) == "degraded"


def test_overall_verdict_one_needs_attention_out_of_three_is_needs_attention():
    # (2+1+2)/3 = 1.667 — below 1.7 → needs_attention
    assert _overall_verdict(["healthy", "needs_attention", "healthy"]) == "needs_attention"


def test_overall_verdict_one_degraded_out_of_22_is_healthy():
    # (21*2 + 0)/22 = 1.909 >= 1.7 → healthy
    verdicts = ["healthy"] * 21 + ["degraded"]
    assert _overall_verdict(verdicts) == "healthy"


def test_overall_verdict_three_degraded_out_of_22_is_healthy():
    # (19*2 + 0)/22 = 1.727 >= 1.7 → healthy
    verdicts = ["healthy"] * 19 + ["degraded"] * 3
    assert _overall_verdict(verdicts) == "healthy"


def test_overall_verdict_four_degraded_out_of_22_is_needs_attention():
    # (18*2 + 0)/22 = 1.636 < 1.7 → needs_attention
    verdicts = ["healthy"] * 18 + ["degraded"] * 4
    assert _overall_verdict(verdicts) == "needs_attention"


def test_overall_verdict_seven_degraded_out_of_22_is_needs_attention():
    # (15*2 + 0)/22 = 1.364 >= 1.3 → needs_attention
    verdicts = ["healthy"] * 15 + ["degraded"] * 7
    assert _overall_verdict(verdicts) == "needs_attention"


def test_overall_verdict_eight_degraded_out_of_22_is_degraded():
    # (14*2 + 0)/22 = 1.273 < 1.3 → degraded
    verdicts = ["healthy"] * 14 + ["degraded"] * 8
    assert _overall_verdict(verdicts) == "degraded"


def test_overall_verdict_above_30pct_not_supervised_returns_not_evaluated():
    # 20 not_supervised out of 22 = 90.9% > 30% → not_evaluated
    verdicts = ["healthy", "degraded"] + [None] * 20
    assert _overall_verdict(verdicts) == SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED


def test_overall_verdict_exactly_at_30pct_not_supervised_does_not_trigger():
    # 3 not_supervised out of 10 = 30% — not strictly more than 30% → normal scoring
    # scores: [2, 2, 2, 2, 2, 2, 2] / 7 = 2.0 → healthy
    verdicts = ["healthy"] * 7 + [None] * 3
    assert _overall_verdict(verdicts) == "healthy"


def test_overall_verdict_just_above_30pct_not_supervised_triggers():
    # 4 not_supervised out of 10 = 40% > 30% → not_evaluated
    verdicts = ["healthy"] * 6 + [None] * 4
    assert _overall_verdict(verdicts) == SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED


# ---------------------------------------------------------------------------
# run_supervisor — project not found
# ---------------------------------------------------------------------------


def test_raises_value_error_when_project_not_found(db_session: Session):
    with pytest.raises(ValueError, match="not found"):
        run_supervisor(db=db_session, project_id=99999)


# ---------------------------------------------------------------------------
# run_supervisor — full happy path with mocked evaluators
# ---------------------------------------------------------------------------


def _patch_evaluators(evaluators: list[MagicMock]):
    return patch(f"{_MODULE}._EVALUATORS", evaluators)


def test_runner_creates_report_and_agent_evaluations(db_session: Session, make_project):
    proj = make_project(name="Runner test project", description="A test project.")
    mock_evals = [
        _make_evaluator_mock("planner", _HEALTHY_OUTPUT),
        _make_evaluator_mock("atomic_task_generator", _HEALTHY_OUTPUT),
    ]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    assert isinstance(report, SupervisorReport)
    assert report.status == SUPERVISOR_REPORT_STATUS_COMPLETED
    assert report.overall_verdict == "healthy"
    assert report.synthesis == _SYNTHESIS_TEXT
    assert report.project_id == proj.id

    evaluations = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id == report.report_id)
        .all()
    )
    assert len(evaluations) == 2
    agent_names = {e.agent_name for e in evaluations}
    assert agent_names == {"planner", "atomic_task_generator"}


def test_runner_degraded_agent_affects_overall_verdict(db_session: Session, make_project):
    # 1 degraded + 1 not_supervised out of 6 = 16.7% not_supervised ≤ 30%
    # scores: [2, 2, 2, 2, 0] / 5 = 1.6 → needs_attention
    proj = make_project(name="Verdict propagation test")
    mock_evals = [
        _make_evaluator_mock("planner", _HEALTHY_OUTPUT),
        _make_evaluator_mock("atomic_task_generator", _HEALTHY_OUTPUT),
        _make_evaluator_mock("execution_sequencer", _HEALTHY_OUTPUT),
        _make_evaluator_mock("environment_planner", _HEALTHY_OUTPUT),
        _make_evaluator_mock("orchestrator", _DEGRADED_OUTPUT),
        _make_evaluator_mock("context_selection_agent", _NOT_SUPERVISED_OUTPUT),
    ]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    assert report.overall_verdict == "needs_attention"


def test_runner_not_supervised_stored_as_null_verdict(db_session: Session, make_project):
    proj = make_project(name="Not-supervised verdict test")
    mock_evals = [_make_evaluator_mock("planner", _NOT_SUPERVISED_OUTPUT)]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        run_supervisor(db=db_session, project_id=proj.id)

    evaluation = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id.isnot(None), AgentEvaluation.agent_name == "planner")
        .first()
    )
    assert evaluation is not None
    assert evaluation.verdict is None
    assert evaluation.findings is None


def test_runner_evaluator_exception_stored_as_null_verdict(db_session: Session, make_project):
    proj = make_project(name="Evaluator exception test")
    failing_mock = MagicMock()
    failing_mock.AGENT_NAME = "bad_agent"
    failing_mock.evaluate.side_effect = RuntimeError("LLM timeout")

    with (
        _patch_evaluators([failing_mock]),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    assert report.status == SUPERVISOR_REPORT_STATUS_COMPLETED

    evaluation = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id == report.report_id)
        .first()
    )
    assert evaluation is not None
    assert evaluation.verdict is None
    assert "LLM timeout" in evaluation.findings


def test_runner_aggregates_run_ids_from_evaluators(db_session: Session, make_project):
    proj = make_project(name="Run IDs aggregation test")
    mock_evals = [
        _make_evaluator_mock(
            "code_change_agent",
            EvaluatorOutput(
                result=AgentEvaluationOutput(
                    verdict="healthy",
                    findings="Code changes were well-structured.",
                    issues=[],
                    suggestions=[],
                ),
                execution_run_ids_analyzed=[1, 2, 3],
                system_versions_seen=["abc123"],
            ),
        ),
        _make_evaluator_mock(
            "command_runner_agent",
            EvaluatorOutput(
                result=AgentEvaluationOutput(
                    verdict="healthy",
                    findings="Commands ran successfully.",
                    issues=[],
                    suggestions=[],
                ),
                execution_run_ids_analyzed=[3, 4, 5],
                system_versions_seen=["abc123", "def456"],
            ),
        ),
    ]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    assert set(report.analyzed_run_ids) == {1, 2, 3, 4, 5}


def test_runner_passes_project_context_to_synthesizer(db_session: Session, make_project):
    proj = make_project(name="My Project", description="Project description here.")
    mock_evals = [_make_evaluator_mock("planner", _HEALTHY_OUTPUT)]
    captured_kwargs: dict = {}

    def capture_synthesize(**kwargs):
        captured_kwargs.update(kwargs)
        return _SYNTHESIS_TEXT

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", side_effect=capture_synthesize),
    ):
        run_supervisor(db=db_session, project_id=proj.id)

    assert captured_kwargs["project_name"] == "My Project"
    assert captured_kwargs["project_description"] == "Project description here."
    assert captured_kwargs["project_id"] == proj.id
    assert captured_kwargs["overall_verdict"] == "healthy"


def test_runner_reads_requirements_draft_from_conversation(db_session: Session, make_project):
    proj = make_project(name="Draft project")
    conv = Conversation(
        project_id=proj.id,
        requirements_draft="Build a REST API with CRUD endpoints.",
    )
    db_session.add(conv)
    db_session.commit()

    mock_evals = [_make_evaluator_mock("planner", _HEALTHY_OUTPUT)]
    captured_kwargs: dict = {}

    def capture_synthesize(**kwargs):
        captured_kwargs.update(kwargs)
        return _SYNTHESIS_TEXT

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", side_effect=capture_synthesize),
    ):
        run_supervisor(db=db_session, project_id=proj.id)

    assert captured_kwargs["requirements_draft"] == "Build a REST API with CRUD endpoints."


def test_runner_synthesis_failure_does_not_crash_report(db_session: Session, make_project):
    proj = make_project(name="Synthesis failure test")
    mock_evals = [_make_evaluator_mock("planner", _HEALTHY_OUTPUT)]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", side_effect=RuntimeError("Synthesis failed")),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    assert report.status == SUPERVISOR_REPORT_STATUS_COMPLETED
    assert "Synthesis generation failed" in report.synthesis


def test_runner_passes_system_version_to_evaluators(
    db_session: Session, make_project, make_task, make_execution_run
):
    proj = make_project(name="System version test")
    task = make_task(project_id=proj.id)
    run = make_execution_run(task_id=task.id)
    run.system_version = "deadbeef"
    db_session.commit()

    mock_eval = MagicMock()
    mock_eval.AGENT_NAME = "planner"
    mock_eval.evaluate.return_value = _HEALTHY_OUTPUT

    with (
        _patch_evaluators([mock_eval]),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        run_supervisor(db=db_session, project_id=proj.id)

    call_kwargs = mock_eval.evaluate.call_args.kwargs
    assert call_kwargs["system_version"] == "deadbeef"


def test_runner_stores_degraded_agent_evaluation_correctly(db_session: Session, make_project):
    proj = make_project(name="Degraded storage test")
    mock_evals = [_make_evaluator_mock("orchestrator", _DEGRADED_OUTPUT)]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    evaluation = (
        db_session.query(AgentEvaluation)
        .filter(AgentEvaluation.report_id == report.report_id)
        .first()
    )
    assert evaluation.verdict == "degraded"
    assert "Systematic failures" in evaluation.findings
    assert evaluation.issues == ["Repeated failure pattern"]
    assert evaluation.suggestions == ["Fix root cause"]


def test_runner_all_not_supervised_gives_not_evaluated_verdict(db_session: Session, make_project):
    # 2 not_supervised out of 2 = 100% > 30% → not_evaluated
    proj = make_project(name="All not supervised test")
    mock_evals = [
        _make_evaluator_mock("planner", _NOT_SUPERVISED_OUTPUT),
        _make_evaluator_mock("orchestrator", _NOT_SUPERVISED_OUTPUT),
    ]

    with (
        _patch_evaluators(mock_evals),
        patch(f"{_MODULE}.synthesize", return_value=_SYNTHESIS_TEXT),
    ):
        report = run_supervisor(db=db_session, project_id=proj.id)

    assert report.overall_verdict == SUPERVISOR_REPORT_VERDICT_NOT_EVALUATED
