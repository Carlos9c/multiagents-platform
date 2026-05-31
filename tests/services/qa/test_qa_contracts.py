"""Tests for QA Engine contracts — Phase 3."""

from __future__ import annotations

from app.services.qa.contracts import (
    VALID_QA_CATEGORIES,
    VALID_QA_SEVERITIES,
    VALID_QA_VERDICTS,
    ProbeRecord,
    QAFindingDetail,
    QARequest,
    QAResult,
    RemediationTask,
)

# ── Constants ─────────────────────────────────────────────────────────────────


def test_severity_constants_present():
    assert VALID_QA_SEVERITIES == {"critical", "high", "medium", "low", "info"}


def test_verdict_constants_present():
    assert VALID_QA_VERDICTS == {"passed", "failed", "blocked", "partial"}


def test_category_constants_present():
    expected = {
        "functional",
        "security",
        "performance",
        "usability",
        "accessibility",
        "compatibility",
        "reliability",
        "data_integrity",
        # Phase 7-9 categories
        "boundary",
        "adversarial",
        "regression",
    }
    assert VALID_QA_CATEGORIES == expected


# ── QARequest ─────────────────────────────────────────────────────────────────


def test_qa_request_minimal():
    req = QARequest(
        project_id=1,
        qa_session_id=10,
        product_type="web_app",
        project_goal="Build a web app",
        source_path="/projects/1/source",
        workspace_path="/projects/1/workspace",
    )
    assert req.project_id == 1
    assert req.artifact_path is None


def test_qa_request_with_artifact_path():
    req = QARequest(
        project_id=2,
        qa_session_id=20,
        product_type="mobile_android",
        project_goal="Build an Android app",
        source_path="/projects/2/source",
        workspace_path="/projects/2/workspace",
        artifact_path="/projects/2/app-debug.apk",
    )
    assert req.artifact_path == "/projects/2/app-debug.apk"


# ── QAFindingDetail ───────────────────────────────────────────────────────────


def test_finding_detail_minimal():
    finding = QAFindingDetail(
        finding_id="f-001",
        severity="high",
        category="security",
        title="SQL injection",
        description="Login endpoint vulnerable",
    )
    assert finding.finding_id == "f-001"
    assert finding.auto_remediable is False
    assert finding.reproduction_steps == []
    assert finding.evidence == {}


def test_finding_detail_full():
    finding = QAFindingDetail(
        finding_id="f-002",
        severity="critical",
        category="functional",
        title="Crash on empty input",
        description="App crashes when input is empty",
        reproduction_steps=["Open app", "Submit empty form"],
        affected_component="SubmitController",
        auto_remediable=True,
        remediation_hint="Add null check before processing",
        evidence={"screenshot": "crash.png"},
    )
    assert finding.auto_remediable is True
    assert len(finding.reproduction_steps) == 2
    assert finding.evidence["screenshot"] == "crash.png"


# ── ProbeRecord ───────────────────────────────────────────────────────────────


def test_probe_record_minimal():
    probe = ProbeRecord(
        agent_name="functional_tester",
        probe_type="smoke_test",
        target="GET /api/health",
        outcome="passed",
    )
    assert probe.findings_count == 0
    assert probe.duration_seconds is None


def test_probe_record_failed():
    probe = ProbeRecord(
        agent_name="security_scanner",
        probe_type="sql_injection",
        target="POST /api/login",
        outcome="failed",
        findings_count=2,
        duration_seconds=3.5,
        notes="Two injection vectors found",
    )
    assert probe.outcome == "failed"
    assert probe.findings_count == 2


# ── RemediationTask ───────────────────────────────────────────────────────────


def test_remediation_task():
    task = RemediationTask(
        finding_id="f-001",
        title="Fix SQL injection in login",
        description="Use parameterised queries",
        priority="critical",
    )
    assert task.estimated_effort == "small"
    assert task.priority == "critical"


# ── QAResult ──────────────────────────────────────────────────────────────────


def test_qa_result_passed_no_findings():
    result = QAResult(verdict="passed")
    assert result.has_findings is False
    assert result.critical_count == 0
    assert result.high_count == 0


def test_qa_result_failed_with_findings():
    findings = [
        QAFindingDetail(
            finding_id="f-001",
            severity="critical",
            category="security",
            title="Critical vuln",
            description="desc",
        ),
        QAFindingDetail(
            finding_id="f-002",
            severity="high",
            category="functional",
            title="High issue",
            description="desc",
        ),
        QAFindingDetail(
            finding_id="f-003",
            severity="medium",
            category="performance",
            title="Medium issue",
            description="desc",
        ),
    ]
    result = QAResult(verdict="failed", findings=findings)
    assert result.has_findings is True
    assert result.critical_count == 1
    assert result.high_count == 1


def test_qa_result_findings_summary():
    findings = [
        QAFindingDetail(
            finding_id=f"f-{i}",
            severity="high",
            category="functional",
            title=f"Issue {i}",
            description="desc",
        )
        for i in range(3)
    ]
    result = QAResult(verdict="failed", findings=findings)
    summary = result.findings_summary()
    assert summary["high"] == 3
    assert summary["critical"] == 0
    assert summary["info"] == 0


def test_qa_result_blocked():
    result = QAResult(verdict="blocked", error_message="APK compilation failed")
    assert result.verdict == "blocked"
    assert result.error_message == "APK compilation failed"
    assert result.has_findings is False
