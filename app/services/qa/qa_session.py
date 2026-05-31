"""QASession — mutable state threaded through the QA orchestrator loop.

Analogous to execution_engine/resolution_state.py (ResolutionState).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.services.qa.contracts import (
    QA_PHASE_DISCOVERY,
    ProbeRecord,
    QAFindingDetail,
    QARequest,
    RemediationTask,
)

QAPhase = Literal["discovery", "probing", "synthesis", "complete"]


class QASession(BaseModel):
    """Mutable state carried through the QA orchestrator loop."""

    request: QARequest

    phase: QAPhase = QA_PHASE_DISCOVERY

    # Agents called so far (for no-repeat and budget tracking)
    agents_called: list[str] = Field(default_factory=list)

    # All probe records from every agent
    probes: list[ProbeRecord] = Field(default_factory=list)

    # All findings accumulated so far
    findings: list[QAFindingDetail] = Field(default_factory=list)

    # Step-level notes (for traceability)
    notes: list[str] = Field(default_factory=list)

    # How many orchestrator steps have run
    step_count: int = 0

    # Set to True when synthesis is forced due to budget exhaustion
    budget_exhausted: bool = False

    # Synthesis agent outputs (written by synthesis_agent, read by orchestrator)
    synthesis_verdict: str | None = None
    synthesis_error: str | None = None
    remediation_tasks: list[RemediationTask] = Field(default_factory=list)

    # ── Mutators ──────────────────────────────────────────────────────────────

    def record_agent_call(self, agent_name: str) -> None:
        self.agents_called.append(agent_name)

    def add_probe(self, probe: ProbeRecord) -> None:
        self.probes.append(probe)

    def add_finding(self, finding: QAFindingDetail) -> None:
        self.findings.append(finding)

    def add_findings(self, findings: list[QAFindingDetail]) -> None:
        self.findings.extend(findings)

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def increment_step(self) -> None:
        self.step_count += 1

    def transition_to_probing(self) -> None:
        self.phase = "probing"

    def transition_to_synthesis(self) -> None:
        self.phase = "synthesis"

    def transition_to_complete(self) -> None:
        self.phase = "complete"

    def mark_budget_exhausted(self) -> None:
        self.budget_exhausted = True

    def set_synthesis_output(
        self,
        *,
        verdict: str,
        remediation_tasks: list[RemediationTask],
    ) -> None:
        self.synthesis_verdict = verdict
        self.remediation_tasks = remediation_tasks

    # ── Queries ───────────────────────────────────────────────────────────────

    def was_called(self, agent_name: str) -> bool:
        return agent_name in self.agents_called

    def call_count(self, agent_name: str) -> int:
        return self.agents_called.count(agent_name)

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def has_critical_findings(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)
