"""Tests for QAOrchestrator — Phase 6 (LLM-driven probing loop).

The orchestrator uses an LLM to decide which agent to call next each round.
All tests mock:
  - app.services.qa.qa_orchestrator.get_llm_provider  (orchestrator LLM decisions)
  - synthesis_agent is provided as a lightweight test double (no LLM needed)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.qa.agents.base import BaseQAAgent, QAAgentError
from app.services.qa.agents.registry import QAAgentRegistry
from app.services.qa.budget import QABudget
from app.services.qa.contracts import (
    QA_VERDICT_FAILED,
    QA_VERDICT_PARTIAL,
    QA_VERDICT_PASSED,
    ProbeRecord,
    QAFindingDetail,
    QARequest,
)
from app.services.qa.qa_orchestrator import QAOrchestrator
from app.services.qa.qa_session import QASession
from app.services.qa.strategies.base import QAStrategy

# ── Decision helpers ──────────────────────────────────────────────────────────


def _call(agent_name: str, reasoning: str = "test") -> dict:
    """Return a dict that validates as OrchestratorDecision(action='call_agent')."""
    return {"action": "call_agent", "agent_name": agent_name, "reasoning": reasoning}


def _finish(reasoning: str = "done") -> dict:
    """Return a dict that validates as OrchestratorDecision(action='finish')."""
    return {"action": "finish", "agent_name": None, "reasoning": reasoning}


# ── LLM mock helpers ──────────────────────────────────────────────────────────


def _patch_orchestrator_llm(*decisions: dict):
    """Patch the orchestrator's LLM to return the given decisions in sequence."""
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = list(decisions)
    return patch(
        "app.services.qa.qa_orchestrator.get_llm_provider",
        return_value=mock_provider,
    )


def _patch_orchestrator_llm_error():
    """Patch the orchestrator's LLM to always raise an exception."""
    mock_provider = MagicMock()
    mock_provider.generate_structured.side_effect = RuntimeError("LLM down")
    return patch(
        "app.services.qa.qa_orchestrator.get_llm_provider",
        return_value=mock_provider,
    )


# ── Request / strategy helpers ────────────────────────────────────────────────


def _make_request(product_type: str = "rest_api") -> QARequest:
    return QARequest(
        project_id=1,
        qa_session_id=10,
        product_type=product_type,
        project_goal="Build a REST API",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )


def _fake_strategy(allowed: list[str]) -> QAStrategy:
    class _Fake(QAStrategy):
        @property
        def product_type(self) -> str:
            return "rest_api"

        @property
        def requires_compiled_artifact(self) -> bool:
            return False

        @property
        def allowed_agents(self) -> list[str]:
            return allowed

    return _Fake()


# ── Agent test doubles ────────────────────────────────────────────────────────


class _NoOpAgent(BaseQAAgent):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def probe(self, *, db, request, session: QASession) -> QASession:
        session.record_agent_call(self.name)
        session.add_probe(
            ProbeRecord(agent_name=self.name, probe_type="smoke", target="all", outcome="passed")
        )
        return session


class _FindingAgent(BaseQAAgent):
    def __init__(self, name: str, n_findings: int = 1, severity: str = "medium") -> None:
        self._name = name
        self._n = n_findings
        self._severity = severity

    @property
    def name(self) -> str:
        return self._name

    def probe(self, *, db, request, session: QASession) -> QASession:
        session.record_agent_call(self.name)
        for i in range(self._n):
            session.add_finding(
                QAFindingDetail(
                    finding_id=f"{self._name}-{i + 1:03d}",
                    severity=self._severity,
                    category="functional",
                    title=f"Issue {i + 1}",
                    description="Test finding",
                )
            )
        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="scan",
                target="all",
                outcome="failed",
                findings_count=self._n,
            )
        )
        return session


class _ErrorAgent(BaseQAAgent):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def probe(self, *, db, request, session: QASession) -> QASession:
        raise QAAgentError("Simulated infrastructure failure")


class _SynthesisAgent(BaseQAAgent):
    """Test-double synthesis agent — sets verdict deterministically without LLM."""

    @property
    def name(self) -> str:
        return "synthesis_agent"

    def probe(self, *, db, request, session: QASession) -> QASession:
        session.record_agent_call(self.name)
        verdict = QA_VERDICT_FAILED if session.findings else QA_VERDICT_PASSED
        session.set_synthesis_output(verdict=verdict, remediation_tasks=[])
        return session


# ── Registry factory ──────────────────────────────────────────────────────────


def _make_registry(*agents: BaseQAAgent) -> QAAgentRegistry:
    return QAAgentRegistry(list(agents))


def _make_orchestrator(
    registry: QAAgentRegistry,
    *,
    max_probe_rounds: int = 20,
    max_probes_per_agent: int = 5,
    timeout_seconds: float = 300.0,
    max_findings: int = 50,
) -> QAOrchestrator:
    budget = QABudget(
        max_probe_rounds=max_probe_rounds,
        max_probes_per_agent=max_probes_per_agent,
        timeout_seconds=timeout_seconds,
        max_findings=max_findings,
    )
    return QAOrchestrator(registry=registry, budget=budget)


# ── Tests — happy path ────────────────────────────────────────────────────────


def test_llm_call_agent_decision_probes_correctly():
    """LLM says call_agent → agent runs → synthesis runs → result is built."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("functional_tester"), _SynthesisAgent())
    strategy = _fake_strategy(["functional_tester", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_call("functional_tester"), _finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "functional_tester" in result.agents_called
    assert "synthesis_agent" in result.agents_called
    assert result.verdict == QA_VERDICT_PASSED


def test_llm_finish_skips_remaining_probing_goes_to_synthesis():
    """LLM immediately says finish → no probing agent runs, synthesis still runs."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("security_scanner"), _SynthesisAgent())
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "security_scanner" not in result.agents_called
    assert "synthesis_agent" in result.agents_called
    assert result.verdict == QA_VERDICT_PASSED


def test_findings_from_probing_appear_in_result():
    """Findings added by probing agents propagate to QAResult."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_FindingAgent("security_scanner", severity="high"), _SynthesisAgent())
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_call("security_scanner"), _finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert len(result.findings) == 1
    assert result.findings[0].severity == "high"
    assert result.verdict == QA_VERDICT_FAILED


def test_multiple_agents_called_in_llm_chosen_order():
    """Orchestrator calls agents in the exact sequence the LLM decides."""
    db = MagicMock(spec=Session)
    call_order: list[str] = []

    class _TracingAgent(BaseQAAgent):
        def __init__(self, agent_name: str) -> None:
            self._name = agent_name

        @property
        def name(self) -> str:
            return self._name

        def probe(self, *, db, request, session: QASession) -> QASession:
            call_order.append(self._name)
            session.record_agent_call(self._name)
            return session

    registry = _make_registry(
        _TracingAgent("security_scanner"),
        _TracingAgent("functional_tester"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["security_scanner", "functional_tester", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(
        _call("security_scanner"),
        _call("functional_tester"),
        _finish(),
    ):
        orc.run(db=db, request=_make_request(), strategy=strategy)

    assert call_order == ["security_scanner", "functional_tester"]


# ── Tests — no-consecutive rule ───────────────────────────────────────────────


def test_no_consecutive_rule_skips_repeated_agent():
    """If LLM tries to call the same agent twice in a row, the second call is skipped."""
    db = MagicMock(spec=Session)
    call_count: dict[str, int] = {}

    class _CountAgent(BaseQAAgent):
        @property
        def name(self) -> str:
            return "functional_tester"

        def probe(self, *, db, request, session: QASession) -> QASession:
            call_count["functional_tester"] = call_count.get("functional_tester", 0) + 1
            session.record_agent_call(self.name)
            return session

    registry = _make_registry(_CountAgent(), _SynthesisAgent())
    strategy = _fake_strategy(["functional_tester", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probe_rounds=5)

    with _patch_orchestrator_llm(
        _call("functional_tester"),  # round 0: runs
        _call("functional_tester"),  # round 1: SKIPPED (consecutive)
        _finish(),  # round 2: finish
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    # Should have been called exactly once despite two LLM decisions
    assert call_count.get("functional_tester", 0) == 1
    # Note about the skip should be in the session
    # (verified indirectly via the result — no crash)
    assert result.verdict == QA_VERDICT_PASSED


def test_no_consecutive_rule_does_not_prevent_non_consecutive_recall():
    """The same agent CAN be called again after a different agent ran between."""
    db = MagicMock(spec=Session)
    call_count: dict[str, int] = {}

    class _CountAgent(BaseQAAgent):
        def __init__(self, agent_name: str) -> None:
            self._name = agent_name

        @property
        def name(self) -> str:
            return self._name

        def probe(self, *, db, request, session: QASession) -> QASession:
            call_count[self._name] = call_count.get(self._name, 0) + 1
            session.record_agent_call(self._name)
            return session

    registry = _make_registry(
        _CountAgent("functional_tester"),
        _CountAgent("security_scanner"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["functional_tester", "security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probe_rounds=5, max_probes_per_agent=3)

    with _patch_orchestrator_llm(
        _call("functional_tester"),  # round 0
        _call("security_scanner"),  # round 1 (different agent — OK)
        _call("functional_tester"),  # round 2 (non-consecutive repeat — allowed)
        _finish(),
    ):
        orc.run(db=db, request=_make_request(), strategy=strategy)

    assert call_count.get("functional_tester", 0) == 2
    assert call_count.get("security_scanner", 0) == 1


# ── Tests — budget enforcement ────────────────────────────────────────────────


def test_max_probe_rounds_forces_termination():
    """When max_probe_rounds is reached, probing stops and synthesis runs."""
    db = MagicMock(spec=Session)
    registry = _make_registry(
        _NoOpAgent("agent_a"),
        _NoOpAgent("agent_b"),
        _NoOpAgent("agent_c"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["agent_a", "agent_b", "agent_c", "synthesis_agent"])
    # Only 2 rounds allowed — agent_c should not run
    orc = _make_orchestrator(registry, max_probe_rounds=2)

    with _patch_orchestrator_llm(
        _call("agent_a"),  # round 0
        _call("agent_b"),  # round 1
        # round 2: budget check fires before LLM call
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "agent_a" in result.agents_called
    assert "agent_b" in result.agents_called
    assert "agent_c" not in result.agents_called
    assert "synthesis_agent" in result.agents_called  # synthesis still runs


def test_max_probe_rounds_sets_budget_exhausted():
    """budget_exhausted flag causes deterministic fallback to 'partial' verdict."""
    db = MagicMock(spec=Session)
    registry = _make_registry(
        _FindingAgent("security_scanner", severity="low"),
        # no synthesis_agent → deterministic verdict from findings
    )
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probe_rounds=1)

    with _patch_orchestrator_llm(_call("security_scanner")):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    # 1 low-severity finding + budget_exhausted → "partial"
    assert result.verdict == QA_VERDICT_PARTIAL


def test_timeout_stops_probing_immediately():
    """With timeout_seconds=0, the loop exits before calling any agent."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("security_scanner"), _SynthesisAgent())
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    # timeout=0 → first elapsed check always fires
    orc = _make_orchestrator(registry, timeout_seconds=0.0)

    with _patch_orchestrator_llm():  # no decisions needed — loop won't reach LLM
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "security_scanner" not in result.agents_called
    assert "synthesis_agent" in result.agents_called  # synthesis still runs after timeout


def test_max_findings_stops_probing_after_threshold():
    """When accumulated findings >= max_findings, probing stops before next LLM call."""
    db = MagicMock(spec=Session)
    registry = _make_registry(
        _FindingAgent("security_scanner", n_findings=5, severity="high"),
        _NoOpAgent("functional_tester"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["security_scanner", "functional_tester", "synthesis_agent"])
    # max_findings=3 → after security_scanner adds 5, loop should stop
    orc = _make_orchestrator(registry, max_findings=3)

    with _patch_orchestrator_llm(
        _call("security_scanner"),  # adds 5 findings → loop stops before calling LLM again
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "security_scanner" in result.agents_called
    assert "functional_tester" not in result.agents_called
    assert len(result.findings) == 5


# ── Tests — constraint enforcement ───────────────────────────────────────────


def test_synthesis_agent_call_during_probing_is_skipped():
    """If LLM tries to call synthesis_agent during probing, it is skipped."""
    db = MagicMock(spec=Session)
    synthesis_call_count: list[int] = [0]

    class _CountingSynthesis(BaseQAAgent):
        @property
        def name(self) -> str:
            return "synthesis_agent"

        def probe(self, *, db, request, session: QASession) -> QASession:
            synthesis_call_count[0] += 1
            session.record_agent_call(self.name)
            session.set_synthesis_output(verdict=QA_VERDICT_PASSED, remediation_tasks=[])
            return session

    registry = _make_registry(_CountingSynthesis())
    strategy = _fake_strategy(["synthesis_agent"])  # only synthesis in allowed
    orc = _make_orchestrator(registry, max_probe_rounds=5)

    with _patch_orchestrator_llm(
        _call("synthesis_agent"),  # round 0: should be skipped
        _call("synthesis_agent"),  # round 1: skipped again
        _finish(),
    ):
        orc.run(db=db, request=_make_request(), strategy=strategy)

    # synthesis_agent should have been called exactly once (synthesis phase only)
    assert synthesis_call_count[0] == 1


def test_agent_not_in_strategy_allowed_is_skipped():
    """LLM choosing an agent not in the strategy is skipped with a note."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("contract_validator"), _SynthesisAgent())
    # contract_validator NOT in strategy allowed list
    strategy = _fake_strategy(["functional_tester", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probe_rounds=5)

    with _patch_orchestrator_llm(
        _call("contract_validator"),  # not in strategy → skipped
        _finish(),
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "contract_validator" not in result.agents_called


def test_agent_not_registered_is_skipped():
    """LLM choosing an agent not in the registry is skipped with a note."""
    db = MagicMock(spec=Session)
    # registry has NO functional_tester
    registry = _make_registry(_SynthesisAgent())
    strategy = _fake_strategy(["functional_tester", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probe_rounds=5)

    with _patch_orchestrator_llm(
        _call("functional_tester"),  # registered? No → skipped
        _finish(),
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "functional_tester" not in result.agents_called
    assert "synthesis_agent" in result.agents_called


def test_per_agent_probe_limit_prevents_excessive_calls():
    """An agent cannot be called more than max_probes_per_agent times."""
    db = MagicMock(spec=Session)
    call_count: list[int] = [0]

    class _CountAgent(BaseQAAgent):
        @property
        def name(self) -> str:
            return "functional_tester"

        def probe(self, *, db, request, session: QASession) -> QASession:
            call_count[0] += 1
            session.record_agent_call(self.name)
            return session

    registry = _make_registry(_CountAgent(), _NoOpAgent("security_scanner"), _SynthesisAgent())
    strategy = _fake_strategy(["functional_tester", "security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probe_rounds=10, max_probes_per_agent=2)

    with _patch_orchestrator_llm(
        _call("functional_tester"),  # call 1
        _call("security_scanner"),  # different agent (reset consecutive)
        _call("functional_tester"),  # call 2
        _call("security_scanner"),  # different agent
        _call("functional_tester"),  # call 3 — OVER LIMIT, skipped
        _finish(),
    ):
        orc.run(db=db, request=_make_request(), strategy=strategy)

    assert call_count[0] == 2


# ── Tests — error resilience ──────────────────────────────────────────────────


def test_agent_qa_error_does_not_abort_loop():
    """QAAgentError from a probing agent is caught; other agents still run."""
    db = MagicMock(spec=Session)
    registry = _make_registry(
        _ErrorAgent("bad_agent"),
        _NoOpAgent("good_agent"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["bad_agent", "good_agent", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(
        _call("bad_agent"),  # raises QAAgentError
        _call("good_agent"),  # still runs
        _finish(),
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "good_agent" in result.agents_called
    assert result.verdict == QA_VERDICT_PASSED


def test_agent_unexpected_error_does_not_abort_loop():
    """An unexpected exception from a probing agent is caught; loop continues."""
    db = MagicMock(spec=Session)

    class _CrashAgent(BaseQAAgent):
        @property
        def name(self) -> str:
            return "crash_agent"

        def probe(self, *, db, request, session: QASession) -> QASession:
            raise RuntimeError("Unexpected crash!")

    registry = _make_registry(
        _CrashAgent(),
        _NoOpAgent("safe_agent"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["crash_agent", "safe_agent", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(
        _call("crash_agent"),  # crashes but loop continues
        _call("safe_agent"),
        _finish(),
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert "safe_agent" in result.agents_called
    assert result.verdict == QA_VERDICT_PASSED


def test_llm_failure_terminates_probing_gracefully():
    """When the LLM decision call raises an exception, probing stops; synthesis runs."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("security_scanner"), _SynthesisAgent())
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm_error():
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    # No agent should have run (LLM failed before first decision)
    assert "security_scanner" not in result.agents_called
    # Synthesis still runs
    assert "synthesis_agent" in result.agents_called
    assert result.verdict == QA_VERDICT_PASSED


# ── Tests — synthesis phase ───────────────────────────────────────────────────


def test_no_synthesis_agent_verdict_derived_deterministically():
    """When synthesis_agent is absent, verdict is derived from findings."""
    db = MagicMock(spec=Session)
    # No synthesis_agent in registry
    registry = _make_registry(_FindingAgent("security_scanner", severity="high"))
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_call("security_scanner"), _finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert result.verdict == QA_VERDICT_FAILED


def test_no_findings_no_synthesis_agent_verdict_is_passed():
    """No findings + no synthesis_agent → verdict 'passed'."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("security_scanner"))
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_call("security_scanner"), _finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert result.verdict == QA_VERDICT_PASSED


def test_synthesis_agent_error_falls_back_to_deterministic_verdict():
    """If synthesis_agent raises, the verdict is derived from findings."""

    class _BrokenSynthesis(BaseQAAgent):
        @property
        def name(self) -> str:
            return "synthesis_agent"

        def probe(self, *, db, request, session: QASession) -> QASession:
            raise RuntimeError("LLM timeout")

    registry = _make_registry(
        _FindingAgent("security_scanner", severity="critical"),
        _BrokenSynthesis(),
    )
    db = MagicMock(spec=Session)
    strategy = _fake_strategy(["security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_call("security_scanner"), _finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert result.verdict == QA_VERDICT_FAILED
    assert result.error_message is not None


# ── Tests — result properties ─────────────────────────────────────────────────


def test_result_contains_duration():
    db = MagicMock(spec=Session)
    registry = _make_registry(_SynthesisAgent())
    strategy = _fake_strategy(["synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    assert result.duration_seconds is not None
    assert result.duration_seconds >= 0.0


def test_agents_called_list_is_deduplicated():
    """agents_called in the result removes duplicates while preserving first-occurrence order."""
    db = MagicMock(spec=Session)
    registry = _make_registry(
        _NoOpAgent("functional_tester"),
        _NoOpAgent("security_scanner"),
        _SynthesisAgent(),
    )
    strategy = _fake_strategy(["functional_tester", "security_scanner", "synthesis_agent"])
    orc = _make_orchestrator(registry, max_probes_per_agent=3)

    with _patch_orchestrator_llm(
        _call("functional_tester"),
        _call("security_scanner"),
        _call("functional_tester"),  # second call allowed (non-consecutive)
        _finish(),
    ):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    # result.agents_called should be deduplicated
    assert result.agents_called.count("functional_tester") == 1
    assert result.agents_called.count("security_scanner") == 1


def test_probes_list_contains_probe_records():
    """Probe records from each agent appear in the result."""
    db = MagicMock(spec=Session)
    registry = _make_registry(_NoOpAgent("functional_tester"), _SynthesisAgent())
    strategy = _fake_strategy(["functional_tester", "synthesis_agent"])
    orc = _make_orchestrator(registry)

    with _patch_orchestrator_llm(_call("functional_tester"), _finish()):
        result = orc.run(db=db, request=_make_request(), strategy=strategy)

    probe_agents = {p.agent_name for p in result.probes}
    assert "functional_tester" in probe_agents
