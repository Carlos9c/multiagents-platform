"""Tests for QASession — Phase 3."""

from __future__ import annotations

from app.services.qa.contracts import (
    ProbeRecord,
    QAFindingDetail,
    QARequest,
)
from app.services.qa.qa_session import QASession


def _make_request() -> QARequest:
    return QARequest(
        project_id=1,
        qa_session_id=5,
        product_type="rest_api",
        project_goal="Build a REST API",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )


def _make_session(**kwargs) -> QASession:
    return QASession(request=_make_request(), **kwargs)


def _make_finding(finding_id: str = "f-001", severity: str = "medium") -> QAFindingDetail:
    return QAFindingDetail(
        finding_id=finding_id,
        severity=severity,
        category="functional",
        title=f"Issue {finding_id}",
        description="A test finding",
    )


def _make_probe(agent_name: str = "functional_tester", outcome: str = "passed") -> ProbeRecord:
    return ProbeRecord(
        agent_name=agent_name,
        probe_type="smoke_test",
        target="GET /api/health",
        outcome=outcome,
    )


# ── Initial state ─────────────────────────────────────────────────────────────


def test_initial_phase_is_discovery():
    s = _make_session()
    assert s.phase == "discovery"


def test_initial_agents_called_empty():
    s = _make_session()
    assert s.agents_called == []


def test_initial_findings_empty():
    s = _make_session()
    assert s.total_findings == 0
    assert s.has_critical_findings is False


def test_initial_step_count_zero():
    s = _make_session()
    assert s.step_count == 0


def test_initial_budget_not_exhausted():
    s = _make_session()
    assert s.budget_exhausted is False


# ── Mutators ──────────────────────────────────────────────────────────────────


def test_record_agent_call():
    s = _make_session()
    s.record_agent_call("functional_tester")
    assert s.was_called("functional_tester")
    assert not s.was_called("security_scanner")


def test_record_agent_call_multiple_times():
    s = _make_session()
    s.record_agent_call("functional_tester")
    s.record_agent_call("functional_tester")
    assert s.call_count("functional_tester") == 2


def test_add_probe():
    s = _make_session()
    probe = _make_probe()
    s.add_probe(probe)
    assert len(s.probes) == 1
    assert s.probes[0].agent_name == "functional_tester"


def test_add_finding():
    s = _make_session()
    finding = _make_finding()
    s.add_finding(finding)
    assert s.total_findings == 1


def test_add_findings_batch():
    s = _make_session()
    findings = [_make_finding(f"f-{i}") for i in range(5)]
    s.add_findings(findings)
    assert s.total_findings == 5


def test_add_note():
    s = _make_session()
    s.add_note("Probe started")
    assert "Probe started" in s.notes


def test_increment_step():
    s = _make_session()
    s.increment_step()
    s.increment_step()
    assert s.step_count == 2


def test_mark_budget_exhausted():
    s = _make_session()
    s.mark_budget_exhausted()
    assert s.budget_exhausted is True


# ── Phase transitions ─────────────────────────────────────────────────────────


def test_transition_to_probing():
    s = _make_session()
    s.transition_to_probing()
    assert s.phase == "probing"


def test_transition_to_synthesis():
    s = _make_session()
    s.transition_to_probing()
    s.transition_to_synthesis()
    assert s.phase == "synthesis"


def test_transition_to_complete():
    s = _make_session()
    s.transition_to_complete()
    assert s.phase == "complete"


# ── Critical findings detection ───────────────────────────────────────────────


def test_has_critical_findings_false_when_only_medium():
    s = _make_session()
    s.add_finding(_make_finding("f-001", severity="medium"))
    assert s.has_critical_findings is False


def test_has_critical_findings_true_when_critical_present():
    s = _make_session()
    s.add_finding(_make_finding("f-001", severity="medium"))
    s.add_finding(_make_finding("f-002", severity="critical"))
    assert s.has_critical_findings is True
