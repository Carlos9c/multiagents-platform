"""Regression QA Agent — detects new findings compared to the previous QA session.

Queries the database for the most recent completed QA session for the same
project, loads its findings, and asks an LLM to semantically compare them
against the findings accumulated so far in the current session.

Findings that appear in the current session but have no semantic equivalent in
the previous session are reported as regression findings (category="regression").
This surfaces *newly introduced* issues that weren't present in the last known-
good QA run.

If no previous completed session exists the probe is skipped — this is expected
behaviour on the very first QA run.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.qa_finding import QAFinding as QAFindingModel
from app.models.qa_session import QA_SESSION_STATUS_COMPLETED
from app.models.qa_session import QASession as QASessionModel
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa.agents._llm_schemas import QAFindingItem, RegressionAnalysisOutput
from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QAFindingDetail, QARequest
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("qa/regression_qa_agent", "analyze_regression")


def _to_finding_detail(item: QAFindingItem, producer_agent: str | None = None) -> QAFindingDetail:
    return QAFindingDetail(
        finding_id=item.finding_id,
        severity=item.severity,
        category=item.category,
        title=item.title,
        description=item.description,
        reproduction_steps=item.reproduction_steps,
        affected_component=item.affected_component,
        auto_remediable=item.auto_remediable,
        remediation_hint=item.remediation_hint,
        producer_agent=producer_agent,
    )


def _serialize_db_findings(findings: list[QAFindingModel]) -> list[dict]:
    """Convert ORM QAFinding rows to plain dicts for the prompt."""
    return [
        {
            "finding_id": f.finding_id,
            "severity": f.severity,
            "category": f.category,
            "title": f.title,
            "description": f.description,
            "affected_component": f.affected_component,
        }
        for f in findings
    ]


def _serialize_current_findings(session: QASession) -> list[dict]:
    """Serialize in-memory QAFindingDetail list to plain dicts for the prompt."""
    return [
        {
            "finding_id": f.finding_id,
            "severity": f.severity,
            "category": f.category,
            "title": f.title,
            "description": f.description,
            "affected_component": f.affected_component,
        }
        for f in session.findings
    ]


class RegressionQAAgent(BaseQAAgent):
    """Compares current QA findings against the previous session to surface regressions."""

    @property
    def name(self) -> str:
        return "regression_qa_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        # ── Find the most recent previous completed session ────────────────────
        prev_session = (
            db.query(QASessionModel)
            .filter(
                QASessionModel.project_id == request.project_id,
                QASessionModel.id < request.qa_session_id,
                QASessionModel.status == QA_SESSION_STATUS_COMPLETED,
            )
            .order_by(QASessionModel.id.desc())
            .first()
        )

        if prev_session is None:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="regression_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="No previous completed QA session found — baseline comparison not possible",
                )
            )
            logger.info(
                "regression_qa_agent_skipped project_id=%s qa_session_id=%s reason=no_baseline",
                request.project_id,
                request.qa_session_id,
            )
            return session

        # ── Load previous findings from DB ─────────────────────────────────────
        prev_findings_rows: list[QAFindingModel] = (
            db.query(QAFindingModel)
            .filter(QAFindingModel.qa_session_id == prev_session.id)
            .order_by(QAFindingModel.id.asc())
            .all()
        )

        # ── Collect current findings from in-memory session ───────────────────
        current_findings = session.findings
        if not current_findings:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="regression_analysis",
                    target=request.source_path,
                    outcome="passed",
                    notes=(
                        f"Previous session #{prev_session.id} had {len(prev_findings_rows)} findings; "
                        "current session has none — all resolved."
                    ),
                )
            )
            logger.info(
                "regression_qa_agent_no_current_findings project_id=%s prev_session=%s",
                request.project_id,
                prev_session.id,
            )
            return session

        # ── LLM semantic comparison ────────────────────────────────────────────
        prev_json = json.dumps(
            _serialize_db_findings(prev_findings_rows), ensure_ascii=False, indent=2
        )
        curr_json = json.dumps(_serialize_current_findings(session), ensure_ascii=False, indent=2)

        prompt_loader.validate_builder_inputs(
            "qa/regression_qa_agent",
            "analyze_regression",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "previous_session_id": str(prev_session.id),
                "previous_findings_json": prev_json,
                "current_findings_json": curr_json,
            },
        )

        user_prompt = (
            f"project_goal: {request.project_goal}\n"
            f"product_type: {request.product_type}\n\n"
            f"Previous QA session ID: {prev_session.id}\n"
            f"Previous findings ({len(prev_findings_rows)}):\n{prev_json}\n\n"
            f"Current findings ({len(current_findings)}):\n{curr_json}\n\n"
            "Identify which current findings are regressions (new since the previous session)."
        )

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="regression_qa_agent_output",
                json_schema=RegressionAnalysisOutput.model_json_schema(),
            )
            output = RegressionAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("regression_qa_agent_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="regression_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes=f"LLM comparison failed: {exc}",
                )
            )
            return session

        # ── Add regression findings to session ────────────────────────────────
        for item in output.new_findings:
            session.add_finding(_to_finding_detail(item, self.name))

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="regression_analysis",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.new_findings),
                notes=(
                    f"Baseline: session #{prev_session.id} "
                    f"({len(prev_findings_rows)} previous findings, "
                    f"{output.resolved_count} resolved). "
                    f"{len(output.new_findings)} regressions detected. {output.probe_summary}"
                ),
            )
        )

        logger.info(
            "regression_qa_agent_complete project_id=%s prev_session=%s "
            "regressions=%s resolved=%s outcome=%s",
            request.project_id,
            prev_session.id,
            len(output.new_findings),
            output.resolved_count,
            output.probe_outcome,
        )
        return session
