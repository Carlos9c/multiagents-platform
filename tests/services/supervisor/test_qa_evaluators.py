"""
Tests for QA agent supervisor evaluators (Phase 7-9) and _qa_evaluation_helpers.

Each evaluator test covers:
- No sessions → returns EvaluatorOutput(result=None) (not_supervised)
- Sessions present → calls LLM and returns AgentEvaluationOutput
- Helper build_qa_agent_evaluation_context() filters correctly by agent_name
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.supervisor.evaluators._qa_evaluation_helpers import (
    build_qa_agent_evaluation_context,
    get_qa_session_ids_analyzed,
)
from app.services.supervisor.evaluators.adversarial_qa_agent_evaluator import (
    AdversarialQAAgentEvaluator,
)
from app.services.supervisor.evaluators.boundary_qa_agent_evaluator import (
    BoundaryQAAgentEvaluator,
)
from app.services.supervisor.evaluators.functional_qa_agent_evaluator import (
    FunctionalQAAgentEvaluator,
)
from app.services.supervisor.evaluators.performance_qa_agent_evaluator import (
    PerformanceQAAgentEvaluator,
)
from app.services.supervisor.evaluators.qa_session_evaluator import QASessionEvaluator
from app.services.supervisor.evaluators.regression_qa_agent_evaluator import (
    RegressionQAAgentEvaluator,
)
from app.services.supervisor.evaluators.security_qa_agent_evaluator import (
    SecurityQAAgentEvaluator,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

_HEALTHY_RESPONSE = {
    "verdict": "healthy",
    "findings": "The QA agent performed well across all sessions.",
    "issues": [],
    "suggestions": [],
}

_DEGRADED_RESPONSE = {
    "verdict": "degraded",
    "findings": "The agent produced speculative findings without code-level evidence.",
    "issues": ["Finding aqa-001 lacked specific code reference"],
    "suggestions": ["Require evidence field for every finding"],
}


def _make_mock_qa_session(
    session_id: int,
    agent_name: str,
    product_type: str = "rest_api",
    verdict: str = "passed",
) -> MagicMock:
    s = MagicMock()
    s.id = session_id
    s.project_id = 1
    s.product_type = product_type
    s.verdict = verdict
    s.agents_called = [agent_name, "synthesis_agent"]
    s.findings_summary = {"high": 0, "medium": 1, "low": 0}
    s.duration_seconds = 12.5
    s.status = "completed"
    # probes must be a plain list so `session.probes or []` works correctly
    s.probes = []
    return s


def _make_mock_qa_finding(
    finding_id: str = "f-001",
    session_id: int = 10,
    severity: str = "medium",
    category: str = "functional",
    producer_agent: str | None = None,
) -> MagicMock:
    f = MagicMock()
    f.id = 1
    f.qa_session_id = session_id
    f.finding_id = finding_id
    f.severity = severity
    f.category = category
    f.title = f"Finding {finding_id}"
    f.description = "A test finding"
    f.affected_component = None
    f.auto_remediable = False
    f.remediation_hint = None
    f.producer_agent = producer_agent
    return f


def _patch_qa_context(agent_name: str, sessions: list = None, findings: list = None):
    """Patch build_qa_agent_evaluation_context to return a canned context."""
    sessions = sessions or []
    ctx = {
        "project_id": 1,
        "agent_name": agent_name,
        "sessions": sessions,
    }
    return patch(
        f"app.services.supervisor.evaluators.{agent_name}_evaluator."
        "build_qa_agent_evaluation_context",
        return_value=ctx,
    )


def _patch_qa_session_context(sessions: list = None):
    """Patch build_qa_session_evaluation_context to return a canned context."""
    sessions = sessions or []
    ctx = {"project_id": 1, "sessions": sessions}
    return patch(
        "app.services.supervisor.evaluators.qa_session_evaluator."
        "build_qa_session_evaluation_context",
        return_value=ctx,
    )


def _patch_qa_context_no_sessions(agent_name: str):
    return _patch_qa_context(agent_name, sessions=[])


def _make_session_data(agent_name: str, session_id: int = 10) -> dict:
    return {
        "session_id": session_id,
        "product_type": "rest_api",
        "verdict": "passed",
        "agents_called": [agent_name, "synthesis_agent"],
        "findings_summary": {},
        "duration_seconds": 10.0,
        "findings": [],
    }


# ── _qa_evaluation_helpers tests ───────────────────────────────────────────────


class TestQAEvaluationHelpers:
    def test_returns_empty_when_no_sessions(self):
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
            []
        )

        ctx = build_qa_agent_evaluation_context(db, project_id=1, agent_name="functional_qa_agent")

        assert ctx["project_id"] == 1
        assert ctx["agent_name"] == "functional_qa_agent"
        assert ctx["sessions"] == []

    def test_filters_sessions_by_agent_name(self):
        """Only sessions where the agent ran are included."""
        db = MagicMock(spec=Session)

        session_with_agent = _make_mock_qa_session(10, "functional_qa_agent")
        session_without_agent = _make_mock_qa_session(11, "security_qa_agent")

        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            session_with_agent,
            session_without_agent,
        ]
        # No findings for either session
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        ctx = build_qa_agent_evaluation_context(db, project_id=1, agent_name="functional_qa_agent")

        # Only the session with functional_qa_agent should appear
        assert len(ctx["sessions"]) == 1
        assert ctx["sessions"][0]["session_id"] == 10

    def test_includes_findings_in_session_data(self):
        """Agent-scoped findings are included under 'agent_findings' key."""
        db = MagicMock(spec=Session)

        session = _make_mock_qa_session(10, "boundary_qa_agent")

        qa_query = MagicMock()
        qa_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            session
        ]

        finding = _make_mock_qa_finding(
            "bqa-BC-001",
            session_id=10,
            category="boundary",
            producer_agent="boundary_qa_agent",
        )

        finding_query = MagicMock()
        finding_query.filter.return_value.order_by.return_value.all.return_value = [finding]

        call_count = [0]

        def _q(model):
            call_count[0] += 1
            return qa_query if call_count[0] == 1 else finding_query

        db.query.side_effect = _q

        ctx = build_qa_agent_evaluation_context(db, project_id=1, agent_name="boundary_qa_agent")

        assert len(ctx["sessions"]) == 1
        # Agent-scoped context uses "agent_findings", not "findings"
        assert len(ctx["sessions"][0]["agent_findings"]) == 1
        assert ctx["sessions"][0]["agent_findings"][0]["finding_id"] == "bqa-BC-001"
        assert ctx["sessions"][0]["agent_findings"][0]["producer_agent"] == "boundary_qa_agent"

    def test_get_qa_session_ids_analyzed(self):
        ctx = {
            "sessions": [
                {"session_id": 5},
                {"session_id": 10},
                {"session_id": 15},
            ]
        }
        ids = get_qa_session_ids_analyzed(ctx)
        assert ids == [5, 10, 15]

    def test_get_qa_session_ids_empty(self):
        ctx = {"sessions": []}
        assert get_qa_session_ids_analyzed(ctx) == []


# ── FunctionalQAAgentEvaluator ─────────────────────────────────────────────────


class TestFunctionalQAAgentEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = FunctionalQAAgentEvaluator()

        with _patch_qa_context_no_sessions("functional_qa_agent"):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_healthy_verdict(self):
        db = MagicMock(spec=Session)
        evaluator = FunctionalQAAgentEvaluator()

        session_data = [_make_session_data("functional_qa_agent", session_id=10)]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

        with (
            _patch_qa_context("functional_qa_agent", sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.functional_qa_agent_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "healthy"
        assert output.execution_run_ids_analyzed == [10]

    def test_agent_name_constant(self):
        assert FunctionalQAAgentEvaluator.AGENT_NAME == "functional_qa_agent"


# ── BoundaryQAAgentEvaluator ───────────────────────────────────────────────────


class TestBoundaryQAAgentEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = BoundaryQAAgentEvaluator()

        with _patch_qa_context_no_sessions("boundary_qa_agent"):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_degraded_verdict(self):
        db = MagicMock(spec=Session)
        evaluator = BoundaryQAAgentEvaluator()

        session_data = [_make_session_data("boundary_qa_agent", session_id=7)]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _DEGRADED_RESPONSE

        with (
            _patch_qa_context("boundary_qa_agent", sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.boundary_qa_agent_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "degraded"

    def test_agent_name_constant(self):
        assert BoundaryQAAgentEvaluator.AGENT_NAME == "boundary_qa_agent"


# ── AdversarialQAAgentEvaluator ────────────────────────────────────────────────


class TestAdversarialQAAgentEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = AdversarialQAAgentEvaluator()

        with _patch_qa_context_no_sessions("adversarial_qa_agent"):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_needs_attention_verdict(self):
        db = MagicMock(spec=Session)
        evaluator = AdversarialQAAgentEvaluator()

        session_data = [_make_session_data("adversarial_qa_agent")]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = {
            "verdict": "needs_attention",
            "findings": "Attack scenarios lacked specificity in two cases.",
            "issues": ["aqa-002 missing code reference"],
            "suggestions": ["Add affected_component to all findings"],
        }

        with (
            _patch_qa_context("adversarial_qa_agent", sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.adversarial_qa_agent_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "needs_attention"
        assert len(output.result.issues) == 1

    def test_agent_name_constant(self):
        assert AdversarialQAAgentEvaluator.AGENT_NAME == "adversarial_qa_agent"


# ── SecurityQAAgentEvaluator ───────────────────────────────────────────────────


class TestSecurityQAAgentEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = SecurityQAAgentEvaluator()

        with _patch_qa_context_no_sessions("security_qa_agent"):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_healthy_verdict(self):
        db = MagicMock(spec=Session)
        evaluator = SecurityQAAgentEvaluator()

        session_data = [_make_session_data("security_qa_agent")]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

        with (
            _patch_qa_context("security_qa_agent", sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.security_qa_agent_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "healthy"

    def test_agent_name_constant(self):
        assert SecurityQAAgentEvaluator.AGENT_NAME == "security_qa_agent"


# ── PerformanceQAAgentEvaluator ────────────────────────────────────────────────


class TestPerformanceQAAgentEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = PerformanceQAAgentEvaluator()

        with _patch_qa_context_no_sessions("performance_qa_agent"):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_verdict_with_session_ids(self):
        db = MagicMock(spec=Session)
        evaluator = PerformanceQAAgentEvaluator()

        session_data = [
            _make_session_data("performance_qa_agent", session_id=3),
            _make_session_data("performance_qa_agent", session_id=8),
        ]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

        with (
            _patch_qa_context("performance_qa_agent", sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.performance_qa_agent_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert set(output.execution_run_ids_analyzed) == {3, 8}

    def test_agent_name_constant(self):
        assert PerformanceQAAgentEvaluator.AGENT_NAME == "performance_qa_agent"


# ── RegressionQAAgentEvaluator ─────────────────────────────────────────────────


class TestRegressionQAAgentEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = RegressionQAAgentEvaluator()

        with _patch_qa_context_no_sessions("regression_qa_agent"):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_healthy_verdict_with_no_regressions(self):
        db = MagicMock(spec=Session)
        evaluator = RegressionQAAgentEvaluator()

        session_data = [_make_session_data("regression_qa_agent", session_id=12)]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = {
            "verdict": "healthy",
            "findings": "Regression detection was accurate with zero false positives.",
            "issues": [],
            "suggestions": [],
        }

        with (
            _patch_qa_context("regression_qa_agent", sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.regression_qa_agent_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "healthy"
        assert output.execution_run_ids_analyzed == [12]

    def test_agent_name_constant(self):
        assert RegressionQAAgentEvaluator.AGENT_NAME == "regression_qa_agent"


# ── QASessionEvaluator ────────────────────────────────────────────────────────


class TestQASessionEvaluator:
    def test_returns_not_supervised_when_no_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = QASessionEvaluator()

        with _patch_qa_session_context(sessions=[]):
            result = evaluator.evaluate(db=db, project_id=1)

        assert result.result is None

    def test_returns_healthy_verdict_with_sessions(self):
        db = MagicMock(spec=Session)
        evaluator = QASessionEvaluator()

        session_data = [
            {
                "session_id": 10,
                "product_type": "cli_tool",
                "verdict": "passed",
                "agents_called": ["functional_qa_agent", "boundary_qa_agent", "security_qa_agent"],
                "findings_summary": {"critical": 0, "high": 0, "medium": 0, "low": 1, "info": 0},
                "duration_seconds": 28.5,
                "findings": [],
            }
        ]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

        with (
            _patch_qa_session_context(sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.qa_session_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "healthy"
        assert output.execution_run_ids_analyzed == [10]

    def test_returns_degraded_verdict_for_single_agent_session(self):
        db = MagicMock(spec=Session)
        evaluator = QASessionEvaluator()

        session_data = [
            {
                "session_id": 5,
                "product_type": "rest_api",
                "verdict": "passed",
                "agents_called": ["functional_qa_agent"],  # only 1 of expected 5
                "findings_summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_seconds": 8.0,
                "findings": [],
            }
        ]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _DEGRADED_RESPONSE

        with (
            _patch_qa_session_context(sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.qa_session_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert output.result is not None
        assert output.result.verdict == "degraded"

    def test_multiple_sessions_all_ids_tracked(self):
        db = MagicMock(spec=Session)
        evaluator = QASessionEvaluator()

        session_data = [
            {
                "session_id": 3,
                "product_type": "cli_tool",
                "verdict": "passed",
                "agents_called": ["functional_qa_agent"],
                "findings_summary": {},
                "duration_seconds": 12.0,
                "findings": [],
            },
            {
                "session_id": 7,
                "product_type": "cli_tool",
                "verdict": "failed",
                "agents_called": ["functional_qa_agent", "security_qa_agent"],
                "findings_summary": {"high": 1},
                "duration_seconds": 25.0,
                "findings": [],
            },
        ]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

        with (
            _patch_qa_session_context(sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.qa_session_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            output = evaluator.evaluate(db=db, project_id=1)

        assert set(output.execution_run_ids_analyzed) == {3, 7}

    def test_agent_name_constant(self):
        assert QASessionEvaluator.AGENT_NAME == "qa_session"

    def test_llm_called_with_session_json(self):
        """The LLM receives the sessions as JSON in the user prompt."""
        db = MagicMock(spec=Session)
        evaluator = QASessionEvaluator()

        session_data = [
            {
                "session_id": 1,
                "product_type": "rest_api",
                "verdict": "passed",
                "agents_called": ["functional_qa_agent"],
                "findings_summary": {},
                "duration_seconds": 10.0,
                "findings": [],
            },
        ]
        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = _HEALTHY_RESPONSE

        with (
            _patch_qa_session_context(sessions=session_data),
            patch(
                "app.services.supervisor.evaluators.qa_session_evaluator.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            evaluator.evaluate(db=db, project_id=99)

        call_kwargs = mock_provider.generate_structured.call_args[1]
        assert "session_id" in call_kwargs["user_prompt"]
        assert "99" in call_kwargs["user_prompt"]


# ── _qa_evaluation_helpers — session-level context builder ────────────────────


class TestBuildQASessionEvaluationContext:
    def test_returns_empty_when_no_sessions(self):
        from app.services.supervisor.evaluators._qa_evaluation_helpers import (
            build_qa_session_evaluation_context,
        )

        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
            []
        )

        ctx = build_qa_session_evaluation_context(db, project_id=1)

        assert ctx["project_id"] == 1
        assert ctx["sessions"] == []

    def test_includes_all_sessions_no_agent_filter(self):
        """build_qa_session_evaluation_context returns sessions from ALL agents."""
        from app.services.supervisor.evaluators._qa_evaluation_helpers import (
            build_qa_session_evaluation_context,
        )

        db = MagicMock(spec=Session)

        session_a = _make_mock_qa_session(10, "functional_qa_agent")
        session_b = _make_mock_qa_session(11, "security_qa_agent")

        session_query = MagicMock()
        session_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            session_a,
            session_b,
        ]

        finding_query = MagicMock()
        finding_query.filter.return_value.order_by.return_value.all.return_value = []

        call_count = [0]

        def _q(model):
            call_count[0] += 1
            return session_query if call_count[0] == 1 else finding_query

        db.query.side_effect = _q

        ctx = build_qa_session_evaluation_context(db, project_id=1)

        # Both sessions must appear (no agent-name filter)
        assert len(ctx["sessions"]) == 2
        session_ids = {s["session_id"] for s in ctx["sessions"]}
        assert session_ids == {10, 11}


# ── supervisor_runner integration check ───────────────────────────────────────


def test_supervisor_runner_includes_qa_evaluators():
    """All 7 QA evaluators (6 agent + 1 session) appear in the _EVALUATORS list."""
    from app.services.supervisor.supervisor_runner import _EVALUATORS

    agent_names = {e.AGENT_NAME for e in _EVALUATORS}
    expected_qa_names = {
        "functional_qa_agent",
        "boundary_qa_agent",
        "adversarial_qa_agent",
        "security_qa_agent",
        "performance_qa_agent",
        "regression_qa_agent",
        "qa_session",
    }
    assert expected_qa_names.issubset(
        agent_names
    ), f"Missing QA evaluators in _EVALUATORS: {expected_qa_names - agent_names}"


def test_supervisor_runner_total_evaluator_count():
    """Total evaluator count is 22 (original) + 6 (QA agents) + 1 (QA session) = 29."""
    from app.services.supervisor.supervisor_runner import _EVALUATORS

    assert len(_EVALUATORS) == 29
