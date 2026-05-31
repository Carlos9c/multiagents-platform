"""Adversarial QA Agent — product-specific attack generation.

Uses an LLM to generate adversarial scenarios tailored to the specific product
type and its implementation.  For each generated attack vector the LLM also
evaluates whether the source code appears vulnerable, producing findings only
for cases with clear code-level evidence.

Unlike the security_scanner (generic OWASP static analysis), this agent focuses
on exploitability: crafting realistic attack payloads and reasoning about the
specific execution paths they would trigger.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa import source_reader
from app.services.qa.agents._llm_schemas import ProbeAnalysisOutput, QAFindingItem
from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QAFindingDetail, QARequest
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("qa/adversarial_qa_agent", "analyze")


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


class AdversarialQAAgent(BaseQAAgent):
    """Generates and evaluates product-specific adversarial attack scenarios."""

    @property
    def name(self) -> str:
        return "adversarial_qa_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        if not files:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="adversarial_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="No source files found for adversarial analysis",
                )
            )
            return session

        source_block = source_reader.format_for_prompt(files)

        prompt_loader.validate_builder_inputs(
            "qa/adversarial_qa_agent",
            "analyze",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
            },
        )

        user_prompt = (
            f"project_goal: {request.project_goal}\n"
            f"product_type: {request.product_type}\n\n"
            f"Source files:\n{source_block}\n\n"
            "Generate adversarial attack scenarios and evaluate code vulnerability."
        )

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="adversarial_qa_agent_output",
                json_schema=ProbeAnalysisOutput.model_json_schema(),
            )
            output = ProbeAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("adversarial_qa_agent_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="adversarial_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes=f"LLM call failed: {exc}",
                )
            )
            return session

        for item in output.findings:
            session.add_finding(_to_finding_detail(item, self.name))

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="adversarial_analysis",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.findings),
                notes=output.probe_summary,
            )
        )

        logger.info(
            "adversarial_qa_agent_complete project_id=%s findings=%s outcome=%s",
            request.project_id,
            len(output.findings),
            output.probe_outcome,
        )
        return session
