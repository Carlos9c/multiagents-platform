"""Security QA Agent — OWASP Top 10 (2021) checklist.

Unlike the existing security_scanner (free-form static analysis), this agent
runs through a *fixed*, version-controlled OWASP checklist.  Each item is
assessed in a single batch LLM call that receives the full checklist and source
code, producing a pass/fail verdict with evidence for every item.

The deterministic checklist structure ensures:
- Comparable results across projects of the same type
- Complete coverage of the OWASP Top 10
- Auditable rationale per item (evidence field)
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa import source_reader
from app.services.qa.agents._llm_schemas import OWASPAnalysisOutput, QAFindingItem
from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QAFindingDetail, QARequest
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = prompt_loader.get("qa/security_qa_agent", "check_owasp")

# ── OWASP Top 10 (2021) ────────────────────────────────────────────────────────

_OWASP_CHECKLIST = [
    {
        "id": "A01:2021",
        "name": "Broken Access Control",
        "description": (
            "Restrictions on authenticated users are not properly enforced. "
            "Check: missing auth checks, IDOR, privilege escalation, CORS misconfiguration."
        ),
    },
    {
        "id": "A02:2021",
        "name": "Cryptographic Failures",
        "description": (
            "Sensitive data exposed due to weak or absent encryption. "
            "Check: hardcoded secrets, weak hash algorithms (MD5/SHA1), unencrypted data at rest."
        ),
    },
    {
        "id": "A03:2021",
        "name": "Injection",
        "description": (
            "User-supplied data sent to an interpreter without proper validation. "
            "Check: SQL injection, command injection, LDAP/XPath injection, template injection."
        ),
    },
    {
        "id": "A04:2021",
        "name": "Insecure Design",
        "description": (
            "Missing or ineffective security controls by design. "
            "Check: no rate limiting, missing input constraints, absent threat modelling artefacts."
        ),
    },
    {
        "id": "A05:2021",
        "name": "Security Misconfiguration",
        "description": (
            "Insecure default configurations or unnecessary features enabled. "
            "Check: debug mode on, permissive CORS, default credentials, verbose error messages."
        ),
    },
    {
        "id": "A06:2021",
        "name": "Vulnerable and Outdated Components",
        "description": (
            "Known-vulnerable libraries or frameworks in use. "
            "Check: pinned versions that have published CVEs, unpinned dependencies."
        ),
    },
    {
        "id": "A07:2021",
        "name": "Identification and Authentication Failures",
        "description": (
            "Weaknesses in authentication and session management. "
            "Check: short/weak tokens, missing brute-force protection, insecure session expiry."
        ),
    },
    {
        "id": "A08:2021",
        "name": "Software and Data Integrity Failures",
        "description": (
            "Code or data that can be tampered with silently. "
            "Check: unsafe pickle/eval/exec usage, unverified plugin/update pipelines."
        ),
    },
    {
        "id": "A09:2021",
        "name": "Security Logging and Monitoring Failures",
        "description": (
            "Insufficient logging to detect and respond to breaches. "
            "Check: no audit log for auth events, secrets logged in plaintext, no error tracing."
        ),
    },
    {
        "id": "A10:2021",
        "name": "Server-Side Request Forgery (SSRF)",
        "description": (
            "Application fetches remote resources without validating the supplied URL. "
            "Check: URL parameters passed to HTTP clients without allowlist validation."
        ),
    },
]


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


class SecurityQAAgent(BaseQAAgent):
    """OWASP Top 10 (2021) checklist-driven security analysis."""

    @property
    def name(self) -> str:
        return "security_qa_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        if not files:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="owasp_checklist",
                    target=request.source_path,
                    outcome="skipped",
                    notes="No source files found for OWASP analysis",
                )
            )
            return session

        source_block = source_reader.format_for_prompt(files)
        checklist_json = json.dumps(_OWASP_CHECKLIST, ensure_ascii=False, indent=2)

        prompt_loader.validate_builder_inputs(
            "qa/security_qa_agent",
            "check_owasp",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
                "owasp_checklist_json": checklist_json,
            },
        )

        user_prompt = (
            f"project_goal: {request.project_goal}\n"
            f"product_type: {request.product_type}\n\n"
            f"Source files:\n{source_block}\n\n"
            f"OWASP Top 10 checklist:\n{checklist_json}\n\n"
            "Assess each checklist item against the source code."
        )

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="security_qa_agent_output",
                json_schema=OWASPAnalysisOutput.model_json_schema(),
            )
            output = OWASPAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("security_qa_agent_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="owasp_checklist",
                    target=request.source_path,
                    outcome="skipped",
                    notes=f"LLM call failed: {exc}",
                )
            )
            return session

        failed_count = sum(1 for r in output.check_results if not r.passed)

        for item in output.findings:
            session.add_finding(_to_finding_detail(item, self.name))

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="owasp_checklist",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.findings),
                notes=(
                    f"OWASP checks: {len(output.check_results)} total, "
                    f"{failed_count} failed. {output.probe_summary}"
                ),
            )
        )

        logger.info(
            "security_qa_agent_complete project_id=%s owasp_checks=%s failed=%s findings=%s",
            request.project_id,
            len(output.check_results),
            failed_count,
            len(output.findings),
        )
        return session
