"""Synthesis agent — the final QA step.

Receives all findings accumulated by probing agents and uses LLM to:
1. Determine the final verdict (passed / partial / failed / blocked)
2. Generate a prioritized remediation task list
3. Write these to the QASession via set_synthesis_output()
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa.agents._llm_schemas import SynthesisOutput
from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QARequest, RemediationTask
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("qa/synthesis_agent", "synthesize")


def _derive_verdict_without_llm(session: QASession) -> str:
    """Fallback verdict derivation when LLM call fails."""
    if not session.findings:
        return "passed"
    if session.budget_exhausted:
        return "partial"
    has_critical_high = any(f.severity in ("critical", "high") for f in session.findings)
    return "failed" if has_critical_high else "partial"


class SynthesisAgentImpl(BaseQAAgent):
    """Synthesizes all QA findings into a final verdict and remediation plan."""

    @property
    def name(self) -> str:
        return "synthesis_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        findings_payload = [
            {
                "finding_id": f.finding_id,
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "description": f.description,
                "affected_component": f.affected_component,
                "auto_remediable": f.auto_remediable,
            }
            for f in session.findings
        ]
        findings_json = json.dumps(findings_payload, indent=2, ensure_ascii=False)

        prompt_loader.validate_builder_inputs(
            "qa/synthesis_agent",
            "synthesize",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "findings_json": findings_json,
                "agents_called": session.agents_called,
                "budget_exhausted": session.budget_exhausted,
            },
        )

        user_prompt = f"""project_goal: {request.project_goal}
product_type: {request.product_type}
agents_called: {session.agents_called}
budget_exhausted: {session.budget_exhausted}

Accumulated findings ({len(session.findings)} total):
{findings_json}

Synthesize the findings into a final verdict and remediation plan."""

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="synthesis_agent_output",
                json_schema=SynthesisOutput.model_json_schema(),
            )
            output = SynthesisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("synthesis_agent_llm_failed exc=%s", exc)
            session.synthesis_error = str(exc)
            # Derive verdict without LLM
            fallback_verdict = _derive_verdict_without_llm(session)
            session.set_synthesis_output(verdict=fallback_verdict, remediation_tasks=[])
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="synthesis",
                    target="all_findings",
                    outcome="skipped",
                    findings_count=len(session.findings),
                    notes=f"LLM synthesis failed: {exc}; fallback verdict={fallback_verdict}",
                )
            )
            return session

        remediation_tasks = [
            RemediationTask(
                finding_id=item.finding_id,
                title=item.title,
                description=item.description,
                priority=item.priority,
                estimated_effort=item.estimated_effort,
            )
            for item in output.remediation_tasks
        ]

        session.set_synthesis_output(
            verdict=output.verdict,
            remediation_tasks=remediation_tasks,
        )

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="synthesis",
                target="all_findings",
                outcome="passed",
                findings_count=len(session.findings),
                notes=output.verdict_rationale[:300] if output.verdict_rationale else None,
            )
        )

        logger.info(
            "synthesis_agent_complete project_id=%s verdict=%s remediation_tasks=%s",
            request.project_id,
            output.verdict,
            len(remediation_tasks),
        )
        return session
