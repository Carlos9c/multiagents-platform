"""
Shared helpers for QA agent supervisor evaluators.

Used by functional_qa_agent_evaluator, boundary_qa_agent_evaluator,
adversarial_qa_agent_evaluator, security_qa_agent_evaluator,
performance_qa_agent_evaluator, and regression_qa_agent_evaluator.

These evaluators read from qa_sessions and qa_findings (not ExecutionRun) so
they need their own context-building path.

build_qa_agent_evaluation_context() is the public entry point.  It returns a
JSON-serialisable dict scoped to the *specific* QA agent: only findings with
``producer_agent == agent_name`` are included, and probe records are filtered to
the agent's own entries from ``session.probes``.  This ensures each evaluator
sees ONLY the evidence produced by its own agent — consistent with the
AGENT_DEVELOPMENT_GUIDE requirement that supervisor evaluators receive
agent-specific evidence.

build_qa_session_evaluation_context() is the public entry point for the
session-level evaluator, which assesses orchestration quality and receives ALL
findings and ALL probes across every agent.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.qa_finding import QAFinding as QAFindingModel
from app.models.qa_session import QA_SESSION_STATUS_COMPLETED
from app.models.qa_session import QASession as QASessionModel

logger = logging.getLogger(__name__)

_MAX_SESSIONS_PER_EVALUATION = 5


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialize_findings(findings: list[QAFindingModel]) -> list[dict[str, Any]]:
    return [
        {
            "finding_id": f.finding_id,
            "severity": f.severity,
            "category": f.category,
            "title": f.title,
            "description": f.description,
            "affected_component": f.affected_component,
            "auto_remediable": f.auto_remediable,
            "remediation_hint": f.remediation_hint,
            "producer_agent": f.producer_agent,
        }
        for f in findings
    ]


def _extract_agent_probes(session: QASessionModel, agent_name: str) -> list[dict[str, Any]]:
    """Return probe records from session.probes that belong to *agent_name*.

    ``session.probes`` is a JSON list of serialised ``ProbeRecord`` dicts
    persisted by ``QAEngine._persist_result``.  Rows with no ``agent_name``
    key are silently skipped.
    """
    raw: list[dict] = session.probes or []
    return [p for p in raw if p.get("agent_name") == agent_name]


def _serialize_session_for_agent(
    session: QASessionModel,
    agent_findings: list[QAFindingModel],
    agent_name: str,
) -> dict[str, Any]:
    """Serialise a session with evidence scoped to one QA agent."""
    return {
        "session_id": session.id,
        "product_type": session.product_type,
        "verdict": session.verdict,
        "agents_called": session.agents_called or [],
        "findings_summary": session.findings_summary or {},
        "duration_seconds": session.duration_seconds,
        # Only this agent's findings
        "agent_findings": _serialize_findings(agent_findings),
        # Only this agent's probe records (outcome, notes, findings_count …)
        "agent_probes": _extract_agent_probes(session, agent_name),
    }


def _serialize_session_full(
    session: QASessionModel,
    findings: list[QAFindingModel],
) -> dict[str, Any]:
    """Serialise a session with ALL findings and ALL probe records.

    Used by ``build_qa_session_evaluation_context``.
    """
    return {
        "session_id": session.id,
        "product_type": session.product_type,
        "verdict": session.verdict,
        "agents_called": session.agents_called or [],
        "findings_summary": session.findings_summary or {},
        "duration_seconds": session.duration_seconds,
        "findings": _serialize_findings(findings),
        "probes": session.probes or [],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_qa_agent_evaluation_context(
    db: Session,
    project_id: int,
    *,
    agent_name: str,
    max_sessions: int = _MAX_SESSIONS_PER_EVALUATION,
) -> dict[str, Any]:
    """Build an evaluation context dict for a QA agent supervisor evaluator.

    Queries the most recent completed QA sessions for this project and filters
    to those where the given *agent_name* participated (appears in agents_called).

    **Evidence is scoped to the agent:** only ``QAFinding`` rows with
    ``producer_agent == agent_name`` are included, and probe records are filtered
    to the agent's own entries from ``session.probes``.

    Returns a JSON-serialisable dict with keys:
      project_id, agent_name, sessions (list of agent-scoped session evidence dicts)

    Returns an empty sessions list if the agent never ran for this project.
    """
    # Fetch recent completed sessions in descending order
    rows: list[QASessionModel] = (
        db.query(QASessionModel)
        .filter(
            QASessionModel.project_id == project_id,
            QASessionModel.status == QA_SESSION_STATUS_COMPLETED,
        )
        .order_by(QASessionModel.id.desc())
        .limit(max_sessions * 3)  # over-fetch; filter by agent participation below
        .all()
    )

    # Filter to sessions where this agent ran.  agents_called is a JSON column —
    # Python-side filter is used because IN-containment queries for JSON arrays
    # are not reliably portable across DB engines.
    participating = [s for s in rows if agent_name in (s.agents_called or [])]
    participating = participating[:max_sessions]

    if not participating:
        return {
            "project_id": project_id,
            "agent_name": agent_name,
            "sessions": [],
        }

    # Load ONLY this agent's findings across all participating sessions.
    # Filtering by producer_agent in the DB query avoids loading unrelated rows.
    session_ids = [s.id for s in participating]
    agent_findings: list[QAFindingModel] = (
        db.query(QAFindingModel)
        .filter(
            QAFindingModel.qa_session_id.in_(session_ids),
            QAFindingModel.producer_agent == agent_name,
        )
        .order_by(QAFindingModel.qa_session_id.asc(), QAFindingModel.id.asc())
        .all()
    )

    # Group agent findings by session_id
    findings_by_session: dict[int, list[QAFindingModel]] = {sid: [] for sid in session_ids}
    for finding in agent_findings:
        if finding.qa_session_id in findings_by_session:
            findings_by_session[finding.qa_session_id].append(finding)

    # Build serialised session data (chronological order — oldest first)
    sessions_data = [
        _serialize_session_for_agent(s, findings_by_session[s.id], agent_name)
        for s in reversed(participating)
    ]

    return {
        "project_id": project_id,
        "agent_name": agent_name,
        "sessions": sessions_data,
    }


def get_qa_session_ids_analyzed(ctx: dict[str, Any]) -> list[int]:
    """Return the session IDs from the context (for EvaluatorOutput tracking)."""
    return [s["session_id"] for s in ctx.get("sessions", [])]


def build_qa_session_evaluation_context(
    db: Session,
    project_id: int,
    *,
    max_sessions: int = _MAX_SESSIONS_PER_EVALUATION,
) -> dict[str, Any]:
    """Build an evaluation context dict for the session-level QA evaluator.

    Unlike ``build_qa_agent_evaluation_context`` this function does NOT filter
    by agent name — it includes every completed QA session for the project with
    ALL findings and ALL probe records.  The session-level evaluator assesses
    orchestration decisions, agent selection breadth, and overall session quality
    rather than one agent's specific behaviour.

    Returns a JSON-serialisable dict with keys:
      project_id, sessions (list of session evidence dicts, oldest first)

    Returns an empty sessions list when no completed sessions exist.
    """
    rows: list[QASessionModel] = (
        db.query(QASessionModel)
        .filter(
            QASessionModel.project_id == project_id,
            QASessionModel.status == QA_SESSION_STATUS_COMPLETED,
        )
        .order_by(QASessionModel.id.desc())
        .limit(max_sessions)
        .all()
    )

    if not rows:
        return {"project_id": project_id, "sessions": []}

    session_ids = [s.id for s in rows]
    all_findings: list[QAFindingModel] = (
        db.query(QAFindingModel)
        .filter(QAFindingModel.qa_session_id.in_(session_ids))
        .order_by(QAFindingModel.qa_session_id.asc(), QAFindingModel.id.asc())
        .all()
    )

    findings_by_session: dict[int, list[QAFindingModel]] = {sid: [] for sid in session_ids}
    for finding in all_findings:
        if finding.qa_session_id in findings_by_session:
            findings_by_session[finding.qa_session_id].append(finding)

    # Present in chronological order (oldest first) for the LLM
    sessions_data = [_serialize_session_full(s, findings_by_session[s.id]) for s in reversed(rows)]

    return {"project_id": project_id, "sessions": sessions_data}
