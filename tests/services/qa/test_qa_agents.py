"""Tests for concrete QA agents — Phase 5.

Each agent is tested with:
- LLM returning findings (happy path)
- LLM failure → graceful skipped probe
- Docker unavailable → source-only analysis or skipped probe
- Specific business logic (e.g. contract_validator skips non-API types)
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.qa.agents._llm_schemas import ProbeAnalysisOutput, QAFindingItem, SynthesisOutput
from app.services.qa.agents.apk_installer import ApkInstallerAgent
from app.services.qa.agents.contract_validator import ContractValidatorAgent
from app.services.qa.agents.functional_tester import FunctionalTesterAgent
from app.services.qa.agents.performance_profiler import PerformanceProfilerAgent
from app.services.qa.agents.security_scanner import SecurityScannerAgent
from app.services.qa.agents.synthesis_agent import SynthesisAgentImpl
from app.services.qa.contracts import QAFindingDetail, QARequest
from app.services.qa.docker_runner import DockerRunResult
from app.services.qa.qa_session import QASession

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_request(product_type: str = "rest_api", source_path: str = "/tmp/proj") -> QARequest:
    return QARequest(
        project_id=1,
        qa_session_id=5,
        product_type=product_type,
        project_goal="Build a REST API with authentication",
        source_path=source_path,
        workspace_path=source_path,
    )


def _make_session(request: QARequest | None = None) -> QASession:
    return QASession(request=request or _make_request())


def _make_finding_item(fid: str = "ft-001", severity: str = "medium") -> QAFindingItem:
    return QAFindingItem(
        finding_id=fid,
        severity=severity,
        category="functional",
        title=f"Issue {fid}",
        description="A test finding",
        reproduction_steps=["Step 1"],
        affected_component=None,
        auto_remediable=False,
        remediation_hint=None,
    )


def _passed_output() -> ProbeAnalysisOutput:
    return ProbeAnalysisOutput(
        findings=[],
        probe_outcome="passed",
        probe_summary="No issues found",
    )


def _failed_output(fid: str = "ft-001") -> ProbeAnalysisOutput:
    return ProbeAnalysisOutput(
        findings=[_make_finding_item(fid)],
        probe_outcome="failed",
        probe_summary="One issue found",
    )


_LLM_AGENT_TARGETS = [
    "app.services.qa.agents.functional_tester.get_llm_provider",
    "app.services.qa.agents.security_scanner.get_llm_provider",
    "app.services.qa.agents.contract_validator.get_llm_provider",
    "app.services.qa.agents.performance_profiler.get_llm_provider",
    "app.services.qa.agents.synthesis_agent.get_llm_provider",
]


def _patch_llm(output: ProbeAnalysisOutput | SynthesisOutput):
    """Patch get_llm_provider in every QA agent module at once."""
    mock_provider = MagicMock()
    mock_provider.generate_structured.return_value = output.model_dump()
    stack = ExitStack()
    for target in _LLM_AGENT_TARGETS:
        stack.enter_context(patch(target, return_value=mock_provider))
    return stack


def _patch_no_docker():
    no_session = DockerRunResult(exit_code=1, stdout="", stderr="no session", no_session=True)
    return patch(
        "app.services.qa.docker_runner.run_in_project_container",
        return_value=no_session,
    )


def _patch_source_reader(files: dict | None = None):
    files = files or {"main.py": "# fake source\ndef hello(): pass"}
    return patch("app.services.qa.source_reader.read_for_analysis", return_value=files)


# ── FunctionalTesterAgent ─────────────────────────────────────────────────────


class TestFunctionalTesterAgent:
    def test_name(self):
        assert FunctionalTesterAgent().name == "functional_tester"

    def test_probe_records_agent_call(self):
        db = MagicMock(spec=Session)
        agent = FunctionalTesterAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            _patch_llm(_passed_output()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("functional_tester")

    def test_probe_adds_findings_from_llm(self):
        db = MagicMock(spec=Session)
        agent = FunctionalTesterAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            _patch_llm(_failed_output("ft-001")),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1
        assert updated.findings[0].finding_id == "ft-001"

    def test_probe_adds_probe_record(self):
        db = MagicMock(spec=Session)
        agent = FunctionalTesterAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            _patch_llm(_passed_output()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert len(updated.probes) == 1
        assert updated.probes[0].agent_name == "functional_tester"

    def test_probe_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = FunctionalTesterAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            patch(
                "app.services.llm.factory.get_llm_provider",
                side_effect=RuntimeError("LLM down"),
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        # Should not raise; records a skipped probe
        assert updated.was_called("functional_tester")
        assert len(updated.probes) == 1
        assert updated.probes[0].outcome == "skipped"
        assert updated.total_findings == 0


# ── SecurityScannerAgent ──────────────────────────────────────────────────────


class TestSecurityScannerAgent:
    def test_name(self):
        assert SecurityScannerAgent().name == "security_scanner"

    def test_probe_no_docker_needed(self):
        """Security scanner works without Docker."""
        db = MagicMock(spec=Session)
        agent = SecurityScannerAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_llm(_passed_output()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("security_scanner")

    def test_probe_skips_when_no_source_files(self):
        db = MagicMock(spec=Session)
        agent = SecurityScannerAgent()
        request = _make_request()
        session = _make_session(request)

        with _patch_source_reader(files={}):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"

    def test_probe_reports_security_findings(self):
        db = MagicMock(spec=Session)
        agent = SecurityScannerAgent()
        request = _make_request()
        session = _make_session(request)

        finding = _make_finding_item("ss-001", severity="critical")
        output = ProbeAnalysisOutput(
            findings=[finding], probe_outcome="failed", probe_summary="SQL injection found"
        )

        with (
            _patch_source_reader(),
            _patch_llm(output),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1
        assert updated.findings[0].severity == "critical"

    def test_probe_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = SecurityScannerAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            patch("app.services.llm.factory.get_llm_provider", side_effect=Exception("LLM down")),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("security_scanner")
        assert updated.probes[0].outcome == "skipped"


# ── ContractValidatorAgent ────────────────────────────────────────────────────


class TestContractValidatorAgent:
    def test_name(self):
        assert ContractValidatorAgent().name == "contract_validator"

    def test_skips_non_api_product_types(self):
        db = MagicMock(spec=Session)
        agent = ContractValidatorAgent()

        for product_type in ["web_app", "cli_tool", "mobile_android", "desktop_app"]:
            request = _make_request(product_type=product_type)
            session = _make_session(request)
            updated = agent.probe(db=db, request=request, session=session)
            assert updated.probes[0].outcome == "skipped", f"Expected skip for {product_type}"

    def test_runs_for_rest_api(self):
        db = MagicMock(spec=Session)
        agent = ContractValidatorAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_llm(_passed_output()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("contract_validator")
        assert updated.probes[0].outcome == "passed"

    def test_runs_for_graphql_api(self):
        db = MagicMock(spec=Session)
        agent = ContractValidatorAgent()
        request = _make_request(product_type="graphql_api")
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_llm(_failed_output("cv-001")),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1

    def test_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = ContractValidatorAgent()
        request = _make_request(product_type="rest_api")
        session = _make_session(request)

        with (
            _patch_source_reader(),
            patch("app.services.llm.factory.get_llm_provider", side_effect=Exception("down")),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"


# ── PerformanceProfilerAgent ──────────────────────────────────────────────────


class TestPerformanceProfilerAgent:
    def test_name(self):
        assert PerformanceProfilerAgent().name == "performance_profiler"

    def test_probe_with_docker_unavailable(self):
        db = MagicMock(spec=Session)
        agent = PerformanceProfilerAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            _patch_llm(_passed_output()),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.was_called("performance_profiler")

    def test_probe_reports_performance_findings(self):
        db = MagicMock(spec=Session)
        agent = PerformanceProfilerAgent()
        request = _make_request()
        session = _make_session(request)

        finding = _make_finding_item("pp-001", severity="medium")
        output = ProbeAnalysisOutput(
            findings=[finding], probe_outcome="failed", probe_summary="N+1 query found"
        )

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            _patch_llm(output),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 1

    def test_graceful_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = PerformanceProfilerAgent()
        request = _make_request()
        session = _make_session(request)

        with (
            _patch_source_reader(),
            _patch_no_docker(),
            patch("app.services.llm.factory.get_llm_provider", side_effect=Exception("down")),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"


# ── SynthesisAgentImpl ────────────────────────────────────────────────────────


class TestSynthesisAgent:
    def test_name(self):
        assert SynthesisAgentImpl().name == "synthesis_agent"

    def test_passed_verdict_when_no_findings(self):
        db = MagicMock(spec=Session)
        agent = SynthesisAgentImpl()
        request = _make_request()
        session = _make_session(request)

        synthesis_output = SynthesisOutput(
            verdict="passed",
            verdict_rationale="No issues found",
            remediation_tasks=[],
        )
        with _patch_llm(synthesis_output):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.synthesis_verdict == "passed"
        assert updated.remediation_tasks == []

    def test_failed_verdict_with_remediation_tasks(self):
        db = MagicMock(spec=Session)
        agent = SynthesisAgentImpl()
        request = _make_request()
        session = _make_session(request)
        # Pre-load a finding
        session.add_finding(
            QAFindingDetail(
                finding_id="ss-001",
                severity="critical",
                category="security",
                title="SQL injection",
                description="Login is vulnerable",
                auto_remediable=True,
                remediation_hint="Use parameterized queries",
            )
        )

        from app.services.qa.agents._llm_schemas import RemediationItem

        synthesis_output = SynthesisOutput(
            verdict="failed",
            verdict_rationale="Critical security issue found",
            remediation_tasks=[
                RemediationItem(
                    finding_id="ss-001",
                    title="Fix SQL injection",
                    description="Use parameterized queries in login endpoint",
                    priority="critical",
                    estimated_effort="small",
                )
            ],
        )

        with _patch_llm(synthesis_output):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.synthesis_verdict == "failed"
        assert len(updated.remediation_tasks) == 1
        assert updated.remediation_tasks[0].finding_id == "ss-001"
        assert updated.remediation_tasks[0].priority == "critical"

    def test_fallback_verdict_on_llm_failure(self):
        db = MagicMock(spec=Session)
        agent = SynthesisAgentImpl()
        request = _make_request()
        session = _make_session(request)
        session.add_finding(
            QAFindingDetail(
                finding_id="ft-001",
                severity="high",
                category="functional",
                title="Broken auth",
                description="Auth bypass possible",
                auto_remediable=False,
                remediation_hint=None,
            )
        )

        with patch("app.services.llm.factory.get_llm_provider", side_effect=Exception("down")):
            updated = agent.probe(db=db, request=request, session=session)

        # Fallback: high severity → failed
        assert updated.synthesis_verdict == "failed"
        assert updated.probes[0].outcome == "skipped"

    def test_fallback_passed_when_no_findings_and_llm_fails(self):
        db = MagicMock(spec=Session)
        agent = SynthesisAgentImpl()
        request = _make_request()
        session = _make_session(request)

        with patch("app.services.llm.factory.get_llm_provider", side_effect=Exception("down")):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.synthesis_verdict == "passed"

    def test_partial_verdict_when_budget_exhausted_and_llm_fails(self):
        db = MagicMock(spec=Session)
        agent = SynthesisAgentImpl()
        request = _make_request()
        session = _make_session(request)
        session.mark_budget_exhausted()
        session.add_finding(
            QAFindingDetail(
                finding_id="ft-001",
                severity="low",
                category="functional",
                title="Minor issue",
                description="Small cosmetic bug",
                auto_remediable=True,
                remediation_hint=None,
            )
        )

        with patch("app.services.llm.factory.get_llm_provider", side_effect=Exception("down")):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.synthesis_verdict == "partial"

    def test_synthesis_agent_sets_probe_record(self):
        db = MagicMock(spec=Session)
        agent = SynthesisAgentImpl()
        request = _make_request()
        session = _make_session(request)

        synthesis_output = SynthesisOutput(
            verdict="passed",
            verdict_rationale="All clear",
            remediation_tasks=[],
        )
        with _patch_llm(synthesis_output):
            updated = agent.probe(db=db, request=request, session=session)

        synthesis_probes = [p for p in updated.probes if p.probe_type == "synthesis"]
        assert len(synthesis_probes) == 1


# ── ApkInstallerAgent ─────────────────────────────────────────────────────────


class TestApkInstallerAgent:
    def test_name(self):
        assert ApkInstallerAgent().name == "apk_installer"

    def test_skips_when_no_artifact_path(self):
        db = MagicMock(spec=Session)
        agent = ApkInstallerAgent()
        request = _make_request(product_type="mobile_android")
        # No artifact_path set
        session = _make_session(request)

        updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "No artifact_path" in (updated.probes[0].notes or "")

    def test_skips_when_no_emulator_connected(self):
        db = MagicMock(spec=Session)
        agent = ApkInstallerAgent()
        request = _make_request(product_type="mobile_android")
        request = request.model_copy(update={"artifact_path": "/tmp/app-debug.apk"})
        session = _make_session(request)

        # adb devices returns no emulator
        with patch(
            "app.services.qa.agents.apk_installer._run_adb",
            return_value=(0, "List of devices attached\n", ""),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.probes[0].outcome == "skipped"
        assert "No running Android emulator" in (updated.probes[0].notes or "")

    def test_records_finding_when_install_fails(self):
        db = MagicMock(spec=Session)
        agent = ApkInstallerAgent()
        request = _make_request(product_type="mobile_android")
        request = request.model_copy(update={"artifact_path": "/tmp/app-debug.apk"})
        session = _make_session(request)

        def _fake_adb(args, timeout=60):
            cmd = args[0] if args else ""
            if cmd == "devices":
                return 0, "List of devices attached\nemulator-5554\tdevice\n", ""
            if cmd == "install":
                return 1, "Failure [INSTALL_FAILED_INVALID_APK]", ""
            return 0, "", ""

        with patch("app.services.qa.agents.apk_installer._run_adb", side_effect=_fake_adb):
            updated = agent.probe(db=db, request=request, session=session)

        install_findings = [f for f in updated.findings if f.finding_id == "apk-001"]
        assert len(install_findings) == 1
        assert install_findings[0].severity == "critical"

    def test_records_finding_when_app_crashes_on_launch(self):
        db = MagicMock(spec=Session)
        agent = ApkInstallerAgent()
        request = _make_request(product_type="mobile_android")
        request = request.model_copy(update={"artifact_path": "/tmp/app-debug.apk"})
        session = _make_session(request)

        def _fake_adb(args, timeout=60):
            cmd = args[0] if args else ""
            if cmd == "devices":
                return 0, "emulator-5554\tdevice\n", ""
            if cmd == "install":
                return 0, "Success", ""
            if cmd == "shell":
                return 1, "** Error in com.example.app (main): android.os.RuntimeException", ""
            return 0, "", ""

        with (
            patch("app.services.qa.agents.apk_installer._run_adb", side_effect=_fake_adb),
            patch(
                "app.services.qa.agents.apk_installer._get_package_name",
                return_value="com.example.app",
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        launch_findings = [f for f in updated.findings if f.finding_id == "apk-002"]
        assert len(launch_findings) == 1

    def test_no_findings_on_successful_install_and_launch(self):
        db = MagicMock(spec=Session)
        agent = ApkInstallerAgent()
        request = _make_request(product_type="mobile_android")
        request = request.model_copy(update={"artifact_path": "/tmp/app-debug.apk"})
        session = _make_session(request)

        def _fake_adb(args, timeout=60):
            cmd = args[0] if args else ""
            if cmd == "devices":
                return 0, "emulator-5554\tdevice\n", ""
            if cmd == "install":
                return 0, "Success", ""
            if cmd == "shell":
                return 0, "Events injected: 1\nDone.", ""
            return 0, "", ""

        with (
            patch("app.services.qa.agents.apk_installer._run_adb", side_effect=_fake_adb),
            patch(
                "app.services.qa.agents.apk_installer._get_package_name",
                return_value="com.example.app",
            ),
        ):
            updated = agent.probe(db=db, request=request, session=session)

        assert updated.total_findings == 0
        passed_probes = [p for p in updated.probes if p.outcome == "passed"]
        assert len(passed_probes) >= 1


# ── build_default_registry ────────────────────────────────────────────────────


def test_build_default_registry_contains_all_agents():
    from app.services.qa.agents.registry import build_default_registry

    registry = build_default_registry()
    # Phase 5 agents
    expected_phase5 = {
        "functional_tester",
        "security_scanner",
        "contract_validator",
        "performance_profiler",
        "apk_installer",
        "synthesis_agent",
    }
    registered = set(registry.all_names())
    assert expected_phase5.issubset(registered)


def test_build_default_registry_synthesis_agent_is_synthesis_impl():
    from app.services.qa.agents.registry import build_default_registry
    from app.services.qa.agents.synthesis_agent import SynthesisAgentImpl

    registry = build_default_registry()
    agent = registry.get("synthesis_agent")
    assert isinstance(agent, SynthesisAgentImpl)
