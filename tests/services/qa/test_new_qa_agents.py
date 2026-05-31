"""Tests for Phase 7-9 QA agents.

Covers:
- FunctionalQAAgent (two-pass LLM)
- BoundaryQAAgent (deterministic corpus + single LLM)
- AdversarialQAAgent (single LLM)
- SecurityQAAgent (OWASP batch LLM)
- PerformanceQAAgent (deterministic thresholds + optional LLM)
- RegressionQAAgent (DB query + semantic LLM comparison)
- registry.build_default_registry() covers new agents
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.qa.agents._llm_schemas import (
    OWASPAnalysisOutput,
    OWASPCheckItem,
    ProbeAnalysisOutput,
    QAFindingItem,
    RegressionAnalysisOutput,
    ScenarioList,
    TestScenario,
)
from app.services.qa.contracts import QAFindingDetail, QARequest
from app.services.qa.docker_runner import DockerRunResult
from app.services.qa.qa_agents.adversarial_qa_agent import AdversarialQAAgent
from app.services.qa.qa_agents.boundary_qa_agent import BoundaryQAAgent
from app.services.qa.qa_agents.functional_qa_agent import FunctionalQAAgent
from app.services.qa.qa_agents.performance_qa_agent import PerformanceQAAgent
from app.services.qa.qa_agents.regression_qa_agent import RegressionQAAgent
from app.services.qa.qa_agents.security_qa_agent import SecurityQAAgent
from app.services.qa.qa_session import QASession

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_request(product_type: str = "rest_api", qa_session_id: int = 10) -> QARequest:
    return QARequest(
        project_id=1,
        qa_session_id=qa_session_id,
        product_type=product_type,
        project_goal="Build a REST API with authentication",
        source_path="/tmp/proj",
        workspace_path="/tmp/proj",
    )


def _make_session(request: QARequest | None = None) -> QASession:
    return QASession(request=request or _make_request())


def _make_finding_item(
    fid: str = "x-001",
    severity: str = "medium",
    category: str = "functional",
) -> QAFindingItem:
    return QAFindingItem(
        finding_id=fid,
        severity=severity,
        category=category,
        title=f"Issue {fid}",
        description="A test finding",
        reproduction_steps=["Step 1"],
        affected_component=None,
        auto_remediable=False,
        remediation_hint=None,
    )


def _passed_probe() -> ProbeAnalysisOutput:
    return ProbeAnalysisOutput(findings=[], probe_outcome="passed", probe_summary="All clear")


def _failed_probe(fid: str = "x-001", category: str = "functional") -> ProbeAnalysisOutput:
    return ProbeAnalysisOutput(
        findings=[_make_finding_item(fid, category=category)],
        probe_outcome="failed",
        probe_summary="Issues found",
    )


def _patch_llm_at(module_path: str, output):
    """Patch get_llm_provider at a specific agent module's import binding."""
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = output.model_dump()
    return patch(module_path, return_value=mock_provider)


def _patch_boundary_llm(output):
    return _patch_llm_at("app.services.qa.qa_agents.boundary_qa_agent.get_llm_provider", output)


def _patch_adversarial_llm(output):
    return _patch_llm_at("app.services.qa.qa_agents.adversarial_qa_agent.get_llm_provider", output)


def _patch_security_llm(output):
    return _patch_llm_at("app.services.qa.qa_agents.security_qa_agent.get_llm_provider", output)


def _patch_performance_llm(output):
    return _patch_llm_at("app.services.qa.qa_agents.performance_qa_agent.get_llm_provider", output)


def _patch_source_reader(files: dict | None = None):
    if files is None:
        files = {"main.py": "# fake source\ndef hello(): pass"}
    return patch("app.services.qa.source_reader.read_for_analysis", return_value=files)


def _patch_no_docker():
    no_session = DockerRunResult(exit_code=1, stdout="", stderr="no session", no_session=True)
    return patch(
        "app.services.qa.docker_runner.run_in_project_container",
        return_value=no_session,
    )


# ── FunctionalQAAgent ──────────────────────────────────────────────────────────


class TestFunctionalQAAgent:
    def test_name(self):
        assert FunctionalQAAgent().name == "functional_qa_agent"

    def test_probe_records_agent_call(self):
        db = MagicMock(spec=Session)
        agent = FunctionalQAAgent()
        request = _make_request()
        session = _make_session(request)

        scenario_list = ScenarioList(
            scenarios=[
                TestScenario(
                    scenario_id="s-001",
                    test_type="happy_path",
                    description="Login with valid credentials",
                    test_inputs="POST /auth/login {username: admin, password: secret}",
                    expected_behavior="Returns 200 with JWT token",
                )
            ],
            coverage_rationale="Covers primary auth flow",
        )
        analysis = _passed_probe()

        mock_provider = MagicMock()
        mock_provider.generate_structured.side_effect = [
            scenario_list.model_dump(),
            analysis.model_dump(),
        ]

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            patch(
                "app.services.qa.qa_agents.functional_qa_agent.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("functional_qa_agent")

    def test_probe_skips_when_no_source(self):
        db = MagicMock(spec=Session)
        agent = FunctionalQAAgent()
        request = _make_request()
        session = _make_session(request)

        with _patch_source_reader(files={}):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert updated.total_findings == 0

    def test_probe_adds_findings_from_pass2(self):
        db = MagicMock(spec=Session)
        agent = FunctionalQAAgent()
        request = _make_request()
        session = _make_session(request)

        scenario_list = ScenarioList(
            scenarios=[
                TestScenario(
                    scenario_id="s-001",
                    test_type="edge_case",
                    description="Empty body",
                    test_inputs="POST /api/resource {}",
                    expected_behavior="Returns 400",
                )
            ],
            coverage_rationale="Edge case coverage",
        )
        analysis = _failed_probe("fqa-001")

        mock_provider = MagicMock()
        mock_provider.generate_structured.side_effect = [
            scenario_list.model_dump(),
            analysis.model_dump(),
        ]

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            patch(
                "app.services.qa.qa_agents.functional_qa_agent.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1
        assert updated.findings[0].finding_id == "fqa-001"

    def test_probe_skips_when_pass1_fails(self):
        db = MagicMock(spec=Session)
        agent = FunctionalQAAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            patch(
                "app.services.qa.qa_agents.functional_qa_agent.get_llm_provider",
                side_effect=RuntimeError("LLM down"),
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("functional_qa_agent")
        assert updated.probes[0].outcome == "skipped"
        assert "Pass 1" in (updated.probes[0].notes or "")

    def test_probe_skips_when_pass2_fails(self):
        db = MagicMock(spec=Session)
        agent = FunctionalQAAgent()
        request = _make_request()
        session = _make_session(request)

        scenario_list = ScenarioList(
            scenarios=[
                TestScenario(
                    scenario_id="s-001",
                    test_type="happy_path",
                    description="Basic test",
                    test_inputs="GET /",
                    expected_behavior="200 OK",
                )
            ],
            coverage_rationale="Coverage note",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.side_effect = [
            scenario_list.model_dump(),
            RuntimeError("Pass 2 LLM down"),
        ]

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            patch(
                "app.services.qa.qa_agents.functional_qa_agent.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "Pass 2" in (updated.probes[0].notes or "")


# ── BoundaryQAAgent ────────────────────────────────────────────────────────────


class TestBoundaryQAAgent:
    def test_name(self):
        assert BoundaryQAAgent().name == "boundary_qa_agent"

    def test_probe_records_agent_call(self):
        db = MagicMock(spec=Session)
        agent = BoundaryQAAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_boundary_llm(_passed_probe()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("boundary_qa_agent")

    def test_probe_skips_when_no_source(self):
        db = MagicMock(spec=Session)
        agent = BoundaryQAAgent()
        request = _make_request()
        session = _make_session(request)

        with _patch_source_reader(files={}):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"

    def test_probe_adds_boundary_findings(self):
        db = MagicMock(spec=Session)
        agent = BoundaryQAAgent()
        request = _make_request()
        session = _make_session(request)

        output = _failed_probe("bqa-BC-001", category="boundary")

        with (
            _patch_source_reader(),
            _patch_boundary_llm(output),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1
        assert updated.findings[0].finding_id == "bqa-BC-001"

    def test_probe_notes_includes_corpus_size(self):
        db = MagicMock(spec=Session)
        agent = BoundaryQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_boundary_llm(_passed_probe()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert "Corpus size:" in (updated.probes[0].notes or "")

    def test_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = BoundaryQAAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.boundary_qa_agent.get_llm_provider",
                side_effect=RuntimeError("LLM down"),
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"


# ── AdversarialQAAgent ─────────────────────────────────────────────────────────


class TestAdversarialQAAgent:
    def test_name(self):
        assert AdversarialQAAgent().name == "adversarial_qa_agent"

    def test_probe_records_agent_call(self):
        db = MagicMock(spec=Session)
        agent = AdversarialQAAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_adversarial_llm(_passed_probe()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("adversarial_qa_agent")

    def test_probe_skips_when_no_source(self):
        db = MagicMock(spec=Session)
        agent = AdversarialQAAgent()
        request = _make_request()
        session = _make_session(request)

        with _patch_source_reader(files={}):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"

    def test_probe_adds_adversarial_findings(self):
        db = MagicMock(spec=Session)
        agent = AdversarialQAAgent()
        request = _make_request()
        session = _make_session(request)

        output = _failed_probe("aqa-001", category="adversarial")

        with (
            _patch_source_reader(),
            _patch_adversarial_llm(output),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1
        assert updated.findings[0].finding_id == "aqa-001"
        assert updated.findings[0].category == "adversarial"

    def test_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = AdversarialQAAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.adversarial_qa_agent.get_llm_provider",
                side_effect=RuntimeError("LLM down"),
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"


# ── SecurityQAAgent ────────────────────────────────────────────────────────────


class TestSecurityQAAgent:
    def test_name(self):
        assert SecurityQAAgent().name == "security_qa_agent"

    def test_probe_skips_when_no_source(self):
        db = MagicMock(spec=Session)
        agent = SecurityQAAgent()
        request = _make_request()
        session = _make_session(request)

        with _patch_source_reader(files={}):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "OWASP" in (updated.probes[0].notes or "")

    def test_probe_with_all_checks_passed(self):
        db = MagicMock(spec=Session)
        agent = SecurityQAAgent()
        request = _make_request()
        session = _make_session(request)

        owasp_output = OWASPAnalysisOutput(
            check_results=[
                OWASPCheckItem(
                    check_id=f"A0{i}:2021",
                    check_name=f"Check {i}",
                    passed=True,
                    evidence="No evidence found",
                )
                for i in range(1, 11)
            ],
            findings=[],
            probe_outcome="passed",
            probe_summary="All checks passed",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = owasp_output.model_dump()

        with (
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.security_qa_agent.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 0
        assert updated.probes[0].outcome == "passed"

    def test_probe_reports_failed_owasp_checks(self):
        db = MagicMock(spec=Session)
        agent = SecurityQAAgent()
        request = _make_request()
        session = _make_session(request)

        security_finding = _make_finding_item("sqa-A01", severity="high", category="security")
        owasp_output = OWASPAnalysisOutput(
            check_results=[
                OWASPCheckItem(
                    check_id="A01:2021",
                    check_name="Broken Access Control",
                    passed=False,
                    evidence="Missing auth check on /admin endpoint",
                ),
                *[
                    OWASPCheckItem(
                        check_id=f"A0{i}:2021",
                        check_name=f"Check {i}",
                        passed=True,
                        evidence="No evidence",
                    )
                    for i in range(2, 11)
                ],
            ],
            findings=[security_finding],
            probe_outcome="failed",
            probe_summary="1 OWASP check failed",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = owasp_output.model_dump()

        with (
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.security_qa_agent.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1
        assert updated.findings[0].category == "security"
        assert "1 failed" in (updated.probes[0].notes or "")

    def test_probe_logs_failed_count(self):
        """Probe record notes report total checks and failure count."""
        db = MagicMock(spec=Session)
        agent = SecurityQAAgent()
        request = _make_request()
        session = _make_session(request)

        owasp_output = OWASPAnalysisOutput(
            check_results=[
                OWASPCheckItem(check_id="A01:2021", check_name="BAC", passed=False, evidence="x"),
                OWASPCheckItem(check_id="A02:2021", check_name="CF", passed=True, evidence="y"),
            ],
            findings=[_make_finding_item("sqa-A01", category="security")],
            probe_outcome="failed",
            probe_summary="Injection detected",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = owasp_output.model_dump()

        with (
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.security_qa_agent.get_llm_provider",
                return_value=mock_provider,
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        notes = updated.probes[0].notes or ""
        assert "2 total" in notes
        assert "1 failed" in notes

    def test_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = SecurityQAAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.security_qa_agent.get_llm_provider",
                side_effect=RuntimeError("LLM down"),
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "LLM call failed" in (updated.probes[0].notes or "")


# ── PerformanceQAAgent ─────────────────────────────────────────────────────────


class TestPerformanceQAAgent:
    def test_name(self):
        assert PerformanceQAAgent().name == "performance_qa_agent"

    def test_threshold_finding_when_startup_exceeds_limit(self):
        """Startup time > 2× threshold generates a high-severity pqa-T001 finding."""
        db = MagicMock(spec=Session)
        agent = PerformanceQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        # Docker returns "real 0m25.000s" — exceeds 10s threshold AND 2× limit (20s)
        docker_result = DockerRunResult(
            exit_code=0,
            stdout="real 0m25.000s\nuser 0m0.100s\nsys 0m0.010s\n",
            stderr="",
        )

        with (
            patch(
                "app.services.qa.qa_agents.performance_qa_agent.run_in_project_container",
                return_value=docker_result,
            ),
            _patch_source_reader(files={}),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        threshold_findings = [f for f in updated.findings if f.finding_id == "pqa-T001"]
        assert len(threshold_findings) == 1
        assert threshold_findings[0].severity == "high"  # 25s > 10s * 2 = 20s → high

    def test_threshold_finding_medium_severity(self):
        """Startup time between threshold and 2× threshold → medium severity."""
        db = MagicMock(spec=Session)
        agent = PerformanceQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        # 12s → exceeds 10s threshold but < 20s (2× threshold) → medium
        docker_result = DockerRunResult(
            exit_code=0,
            stdout="real 0m12.000s\n",
            stderr="",
        )

        with (
            patch(
                "app.services.qa.qa_agents.performance_qa_agent.run_in_project_container",
                return_value=docker_result,
            ),
            _patch_source_reader(files={}),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        threshold_findings = [f for f in updated.findings if f.finding_id == "pqa-T001"]
        assert len(threshold_findings) == 1
        assert threshold_findings[0].severity == "medium"

    def test_no_threshold_finding_within_limit(self):
        """Startup time within threshold → no pqa-T001 finding."""
        db = MagicMock(spec=Session)
        agent = PerformanceQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        # 5s → within 10s limit
        docker_result = DockerRunResult(
            exit_code=0,
            stdout="real 0m5.000s\n",
            stderr="",
        )

        with (
            patch(
                "app.services.qa.qa_agents.performance_qa_agent.run_in_project_container",
                return_value=docker_result,
            ),
            _patch_source_reader(files={}),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        threshold_findings = [f for f in updated.findings if f.finding_id == "pqa-T001"]
        assert len(threshold_findings) == 0

    def test_docker_unavailable_continues_to_llm(self):
        """When Docker is unavailable, timing is skipped but LLM source analysis runs."""
        db = MagicMock(spec=Session)
        agent = PerformanceQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        llm_output = _failed_probe("pqa-S001", category="performance")

        with (
            _patch_no_docker(),
            _patch_source_reader(),
            patch(
                "app.services.qa.qa_agents.performance_qa_agent.get_llm_provider",
                return_value=MagicMock(
                    generate_structured=MagicMock(return_value=llm_output.model_dump())
                ),
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("performance_qa_agent")
        # LLM findings are added even when timing unavailable
        llm_findings = [f for f in updated.findings if f.finding_id == "pqa-S001"]
        assert len(llm_findings) == 1

    def test_zsh_timing_format_parsed(self):
        """zsh 'N.NNN real' timing format is parsed correctly."""
        db = MagicMock(spec=Session)
        agent = PerformanceQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        # zsh format: "12.345 real"
        docker_result = DockerRunResult(
            exit_code=0,
            stdout="12.345 real\n",
            stderr="",
        )

        with (
            patch(
                "app.services.qa.qa_agents.performance_qa_agent.run_in_project_container",
                return_value=docker_result,
            ),
            _patch_source_reader(files={}),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        # 12.345s > 10s threshold → should generate a finding
        threshold_findings = [f for f in updated.findings if f.finding_id == "pqa-T001"]
        assert len(threshold_findings) == 1

    def test_no_source_files_only_timing_performed(self):
        """Without source files, only timing measurements are performed."""
        db = MagicMock(spec=Session)
        agent = PerformanceQAAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        docker_result = DockerRunResult(exit_code=0, stdout="real 0m5.000s\n", stderr="")

        with (
            patch(
                "app.services.qa.qa_agents.performance_qa_agent.run_in_project_container",
                return_value=docker_result,
            ),
            _patch_source_reader(files={}),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].probe_type == "performance_analysis"
        assert "only timing" in (updated.probes[0].notes or "").lower()


# ── RegressionQAAgent ──────────────────────────────────────────────────────────


class TestRegressionQAAgent:
    def test_name(self):
        assert RegressionQAAgent().name == "regression_qa_agent"

    def test_probe_skips_when_no_previous_session(self):
        """First-ever session has no baseline — probe should be skipped."""
        db = MagicMock(spec=Session)
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        agent = RegressionQAAgent()
        request = _make_request(qa_session_id=1)
        session = _make_session(request)

        updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "No previous" in (updated.probes[0].notes or "")
        assert updated.total_findings == 0

    def test_probe_passed_when_no_current_findings(self):
        """If current session has no findings, all previous are resolved → passed."""
        prev_session = MagicMock()
        prev_session.id = 5

        db = MagicMock(spec=Session)
        # First query: find previous session
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
            prev_session
        )
        # Second query: previous findings (2 items)
        prev_finding = MagicMock()
        prev_finding.id = 1
        prev_finding.qa_session_id = 5
        prev_finding.finding_id = "ss-001"
        prev_finding.severity = "high"
        prev_finding.category = "security"
        prev_finding.title = "Old finding"
        prev_finding.description = "Was there before"
        prev_finding.affected_component = None
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            prev_finding
        ]

        agent = RegressionQAAgent()
        request = _make_request(qa_session_id=10)
        session = _make_session(request)
        # No findings in current session

        updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "passed"
        assert "all resolved" in (updated.probes[0].notes or "").lower()

    def test_probe_detects_regressions(self):
        """New findings in current session → LLM identifies them as regressions."""
        prev_session = MagicMock()
        prev_session.id = 5

        prev_finding = MagicMock()
        prev_finding.id = 1
        prev_finding.qa_session_id = 5
        prev_finding.finding_id = "ss-001"
        prev_finding.severity = "medium"
        prev_finding.category = "security"
        prev_finding.title = "Pre-existing issue"
        prev_finding.description = "Was there before"
        prev_finding.affected_component = None

        # Set up db mock chain
        db = MagicMock(spec=Session)
        # We need to handle two separate db.query() calls differently.
        # Use side_effect on the query chain.
        mock_qa_session_query = MagicMock()
        mock_qa_session_query.filter.return_value.order_by.return_value.first.return_value = (
            prev_session
        )
        mock_qa_finding_query = MagicMock()
        mock_qa_finding_query.filter.return_value.order_by.return_value.all.return_value = [
            prev_finding
        ]

        call_count = [0]

        def _query_side_effect(model):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_qa_session_query
            return mock_qa_finding_query

        db.query.side_effect = _query_side_effect

        agent = RegressionQAAgent()
        request = _make_request(qa_session_id=10)
        session = _make_session(request)

        # Add a pre-existing finding (same as previous session)
        session.add_finding(
            QAFindingDetail(
                finding_id="ss-001",
                severity="medium",
                category="security",
                title="Pre-existing issue",
                description="Was there before",
                auto_remediable=False,
            )
        )
        # Add a NEW finding (regression)
        session.add_finding(
            QAFindingDetail(
                finding_id="fqa-001",
                severity="high",
                category="functional",
                title="New authentication bug",
                description="New issue introduced in this build",
                auto_remediable=False,
            )
        )

        regression_output = RegressionAnalysisOutput(
            new_findings=[
                QAFindingItem(
                    finding_id="rqa-001",
                    severity="high",
                    category="regression",
                    title="[Regression] New authentication bug",
                    description="This finding is new since session #5",
                    reproduction_steps=["Step 1"],
                    affected_component=None,
                    auto_remediable=False,
                    remediation_hint=None,
                )
            ],
            resolved_count=0,
            probe_outcome="failed",
            probe_summary="1 regression detected",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = regression_output.model_dump()

        with patch(
            "app.services.qa.qa_agents.regression_qa_agent.get_llm_provider",
            return_value=mock_provider,
        ):
            updated = agent.probe(db=db, request=request, session=session)

        regression_findings = [f for f in updated.findings if f.category == "regression"]
        assert len(regression_findings) == 1
        assert regression_findings[0].finding_id == "rqa-001"
        assert updated.probes[0].outcome == "failed"

    def test_probe_passed_when_no_regressions(self):
        """All current findings already existed → no regressions → passed."""
        prev_session = MagicMock()
        prev_session.id = 5

        prev_finding = MagicMock()
        prev_finding.id = 1
        prev_finding.qa_session_id = 5
        prev_finding.finding_id = "ss-001"
        prev_finding.severity = "medium"
        prev_finding.category = "security"
        prev_finding.title = "Known issue"
        prev_finding.description = "Was there before"
        prev_finding.affected_component = None

        db = MagicMock(spec=Session)
        call_count = [0]
        mock_qa_session_q = MagicMock()
        mock_qa_session_q.filter.return_value.order_by.return_value.first.return_value = (
            prev_session
        )
        mock_qa_finding_q = MagicMock()
        mock_qa_finding_q.filter.return_value.order_by.return_value.all.return_value = [
            prev_finding
        ]

        def _q(model):
            call_count[0] += 1
            return mock_qa_session_q if call_count[0] == 1 else mock_qa_finding_q

        db.query.side_effect = _q

        agent = RegressionQAAgent()
        request = _make_request(qa_session_id=10)
        session = _make_session(request)
        session.add_finding(
            QAFindingDetail(
                finding_id="ss-001",
                severity="medium",
                category="security",
                title="Known issue",
                description="Same as before",
                auto_remediable=False,
            )
        )

        no_regression_output = RegressionAnalysisOutput(
            new_findings=[],
            resolved_count=0,
            probe_outcome="passed",
            probe_summary="No regressions detected",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = no_regression_output.model_dump()

        with patch(
            "app.services.qa.qa_agents.regression_qa_agent.get_llm_provider",
            return_value=mock_provider,
        ):
            updated = agent.probe(db=db, request=request, session=session)

        # No new regression findings added
        regression_findings = [f for f in updated.findings if f.category == "regression"]
        assert len(regression_findings) == 0
        assert updated.probes[0].outcome == "passed"

    def test_graceful_on_llm_failure(self):
        prev_session = MagicMock()
        prev_session.id = 5

        prev_finding = MagicMock()
        prev_finding.id = 1
        prev_finding.qa_session_id = 5
        prev_finding.finding_id = "ss-001"
        prev_finding.severity = "medium"
        prev_finding.category = "security"
        prev_finding.title = "Old issue"
        prev_finding.description = "Old"
        prev_finding.affected_component = None

        db = MagicMock(spec=Session)
        call_count = [0]
        mock_q_session = MagicMock()
        mock_q_session.filter.return_value.order_by.return_value.first.return_value = prev_session
        mock_q_finding = MagicMock()
        mock_q_finding.filter.return_value.order_by.return_value.all.return_value = [prev_finding]

        def _q(model):
            call_count[0] += 1
            return mock_q_session if call_count[0] == 1 else mock_q_finding

        db.query.side_effect = _q

        agent = RegressionQAAgent()
        request = _make_request(qa_session_id=10)
        session = _make_session(request)
        session.add_finding(
            QAFindingDetail(
                finding_id="new-001",
                severity="high",
                category="functional",
                title="New issue",
                description="New",
                auto_remediable=False,
            )
        )

        with patch(
            "app.services.qa.qa_agents.regression_qa_agent.get_llm_provider",
            side_effect=RuntimeError("LLM down"),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "LLM comparison failed" in (updated.probes[0].notes or "")


# ── Registry ───────────────────────────────────────────────────────────────────


def test_build_default_registry_contains_new_agents():
    from app.services.qa.agents.registry import build_default_registry

    registry = build_default_registry()
    new_agents = {
        "functional_qa_agent",
        "boundary_qa_agent",
        "adversarial_qa_agent",
        "security_qa_agent",
        "performance_qa_agent",
        "regression_qa_agent",
    }
    registered = set(registry.all_names())
    assert new_agents.issubset(registered), f"Missing agents: {new_agents - registered}"


def test_build_default_registry_total_agent_count():
    from app.services.qa.agents.registry import build_default_registry

    registry = build_default_registry()
    # 6 Phase 5 agents + 6 Phase 7-9 agents = 12
    assert len(registry.all_names()) == 12
