"""Security scanner QA agent.

Performs static source code analysis to detect common security vulnerabilities
(OWASP Top 10 and mobile-specific issues). No Docker required.
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

_SYSTEM_PROMPT = prompt_loader.get("qa/security_scanner", "analyze")


def _to_finding_detail(item: QAFindingItem) -> QAFindingDetail:
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
    )


class SecurityScannerAgent(BaseQAAgent):
    """Identifies security vulnerabilities via static source code analysis."""

    @property
    def name(self) -> str:
        return "security_scanner"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        source_block = source_reader.format_for_prompt(files)

        if not files:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="static_security_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="No source files available for analysis",
                )
            )
            return session

        prompt_loader.validate_builder_inputs(
            "qa/security_scanner",
            "analyze",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
            },
        )

        user_prompt = f"""project_goal: {request.project_goal}
product_type: {request.product_type}

Source files:
{source_block}

Scan for security vulnerabilities."""

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="security_scanner_output",
                json_schema=ProbeAnalysisOutput.model_json_schema(),
            )
            output = ProbeAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("security_scanner_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="static_security_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes=f"LLM call failed: {exc}",
                )
            )
            return session

        for item in output.findings:
            session.add_finding(_to_finding_detail(item))

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="static_security_analysis",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.findings),
                notes=output.probe_summary,
            )
        )

        logger.info(
            "security_scanner_complete project_id=%s findings=%s outcome=%s",
            request.project_id,
            len(output.findings),
            output.probe_outcome,
        )
        return session
