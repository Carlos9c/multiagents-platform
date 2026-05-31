"""QA Engine contracts.

All Pydantic models used as input/output across the QA orchestrator loop
and its agents. Analogous to execution_engine/contracts.py.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Severity / category constants ─────────────────────────────────────────────

QA_SEVERITY_CRITICAL = "critical"
QA_SEVERITY_HIGH = "high"
QA_SEVERITY_MEDIUM = "medium"
QA_SEVERITY_LOW = "low"
QA_SEVERITY_INFO = "info"

VALID_QA_SEVERITIES = {
    QA_SEVERITY_CRITICAL,
    QA_SEVERITY_HIGH,
    QA_SEVERITY_MEDIUM,
    QA_SEVERITY_LOW,
    QA_SEVERITY_INFO,
}

QA_CATEGORY_FUNCTIONAL = "functional"
QA_CATEGORY_SECURITY = "security"
QA_CATEGORY_PERFORMANCE = "performance"
QA_CATEGORY_USABILITY = "usability"
QA_CATEGORY_ACCESSIBILITY = "accessibility"
QA_CATEGORY_COMPATIBILITY = "compatibility"
QA_CATEGORY_RELIABILITY = "reliability"
QA_CATEGORY_DATA_INTEGRITY = "data_integrity"
QA_CATEGORY_BOUNDARY = "boundary"
QA_CATEGORY_ADVERSARIAL = "adversarial"
QA_CATEGORY_REGRESSION = "regression"

VALID_QA_CATEGORIES = {
    QA_CATEGORY_FUNCTIONAL,
    QA_CATEGORY_SECURITY,
    QA_CATEGORY_PERFORMANCE,
    QA_CATEGORY_USABILITY,
    QA_CATEGORY_ACCESSIBILITY,
    QA_CATEGORY_COMPATIBILITY,
    QA_CATEGORY_RELIABILITY,
    QA_CATEGORY_DATA_INTEGRITY,
    QA_CATEGORY_BOUNDARY,
    QA_CATEGORY_ADVERSARIAL,
    QA_CATEGORY_REGRESSION,
}

# ── Verdict / phase constants ──────────────────────────────────────────────────

QA_VERDICT_PASSED = "passed"
QA_VERDICT_FAILED = "failed"
QA_VERDICT_BLOCKED = "blocked"
QA_VERDICT_PARTIAL = "partial"

VALID_QA_VERDICTS = {
    QA_VERDICT_PASSED,
    QA_VERDICT_FAILED,
    QA_VERDICT_BLOCKED,
    QA_VERDICT_PARTIAL,
}

QA_PHASE_DISCOVERY = "discovery"
QA_PHASE_PROBING = "probing"
QA_PHASE_SYNTHESIS = "synthesis"
QA_PHASE_COMPLETE = "complete"

VALID_QA_PHASES = {
    QA_PHASE_DISCOVERY,
    QA_PHASE_PROBING,
    QA_PHASE_SYNTHESIS,
    QA_PHASE_COMPLETE,
}

# ── QA Request ────────────────────────────────────────────────────────────────


class QARequest(BaseModel):
    """Input to the QA Engine for a single QA run."""

    project_id: int
    qa_session_id: int
    product_type: str
    project_goal: str
    source_path: str
    workspace_path: str

    # Set by QABootstrapper if compilation was required
    artifact_path: str | None = None


# ── Evidence models ───────────────────────────────────────────────────────────


class QAFindingDetail(BaseModel):
    """A single finding discovered during a QA probe."""

    finding_id: str
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal[
        "functional",
        "security",
        "performance",
        "usability",
        "accessibility",
        "compatibility",
        "reliability",
        "data_integrity",
        "boundary",
        "adversarial",
        "regression",
    ]
    title: str
    description: str
    reproduction_steps: list[str] = Field(default_factory=list)
    affected_component: str | None = None
    auto_remediable: bool = False
    remediation_hint: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    # Set by each QA agent to attribute the finding — used by supervisor evaluators.
    producer_agent: str | None = None


class ProbeRecord(BaseModel):
    """Record of a single probe executed by a QA agent."""

    agent_name: str
    probe_type: str
    target: str
    outcome: Literal["passed", "failed", "blocked", "skipped"]
    duration_seconds: float | None = None
    findings_count: int = 0
    notes: str | None = None
    raw_output: str | None = None


class RemediationTask(BaseModel):
    """A proposed remediation task derived from QA findings."""

    finding_id: str
    title: str
    description: str
    priority: Literal["critical", "high", "medium", "low"]
    estimated_effort: Literal["trivial", "small", "medium", "large"] = "small"


# ── QA Result ─────────────────────────────────────────────────────────────────


class QAResult(BaseModel):
    """Final output from the QA Engine after all agents have run."""

    verdict: Literal["passed", "failed", "blocked", "partial"]
    findings: list[QAFindingDetail] = Field(default_factory=list)
    probes: list[ProbeRecord] = Field(default_factory=list)
    remediation_tasks: list[RemediationTask] = Field(default_factory=list)
    agents_called: list[str] = Field(default_factory=list)
    duration_seconds: float | None = None
    error_message: str | None = None

    @property
    def has_findings(self) -> bool:
        return len(self.findings) > 0

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == QA_SEVERITY_CRITICAL)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == QA_SEVERITY_HIGH)

    def findings_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {s: 0 for s in VALID_QA_SEVERITIES}
        for finding in self.findings:
            summary[finding.severity] = summary.get(finding.severity, 0) + 1
        return summary
