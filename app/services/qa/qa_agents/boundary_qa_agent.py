"""Boundary QA Agent — deterministic corpus + LLM analysis.

The *corpus* is a pre-defined, product-type-specific catalogue of boundary
test cases (empty inputs, null fields, integer limits, oversized payloads,
special characters, etc.).  The LLM does not invent the test cases — it only
analyses the source code to determine which corpus items the implementation
handles incorrectly.

This two-phase design gives reproducible, comparable results across projects
of the same product type.
"""

from __future__ import annotations

import json
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

_SYSTEM_PROMPT = prompt_loader.get("qa/boundary_qa_agent", "analyze")

# ── Boundary corpus ────────────────────────────────────────────────────────────

_BOUNDARY_CORPUS: dict[str, list[dict]] = {
    "rest_api": [
        {"id": "BC-001", "name": "empty_body", "description": "POST with empty request body {}"},
        {
            "id": "BC-002",
            "name": "null_required_field",
            "description": "Required field set to null",
        },
        {
            "id": "BC-003",
            "name": "oversized_string",
            "description": "String field with 10,000+ characters",
        },
        {"id": "BC-004", "name": "negative_id", "description": "Resource ID = -1 in URL path"},
        {"id": "BC-005", "name": "zero_id", "description": "Resource ID = 0"},
        {"id": "BC-006", "name": "max_int_id", "description": "Resource ID = 2,147,483,647"},
        {
            "id": "BC-007",
            "name": "sql_metacharacters",
            "description": "Field containing: ' \" ; -- DROP TABLE",
        },
        {
            "id": "BC-008",
            "name": "unicode_control_chars",
            "description": "Field with Unicode RTL override and NUL byte",
        },
        {
            "id": "BC-009",
            "name": "missing_auth_header",
            "description": "Request without Authorization header",
        },
        {
            "id": "BC-010",
            "name": "malformed_json",
            "description": "Request body is invalid JSON (truncated)",
        },
        {
            "id": "BC-011",
            "name": "wrong_content_type",
            "description": "Content-Type: text/plain instead of application/json",
        },
        {
            "id": "BC-012",
            "name": "duplicate_keys",
            "description": "JSON body with duplicate key names",
        },
    ],
    "graphql_api": [
        {
            "id": "BC-001",
            "name": "null_required_arg",
            "description": "Required GraphQL argument set to null",
        },
        {
            "id": "BC-002",
            "name": "deeply_nested_query",
            "description": "Query nested 10+ levels deep",
        },
        {
            "id": "BC-003",
            "name": "oversized_string_arg",
            "description": "String argument with 10,000+ characters",
        },
        {"id": "BC-004", "name": "negative_pagination", "description": "first/limit argument = -1"},
        {
            "id": "BC-005",
            "name": "empty_selection_set",
            "description": "Query with no fields in selection set",
        },
        {
            "id": "BC-006",
            "name": "injection_in_variable",
            "description": "Variable containing SQL/JS injection",
        },
    ],
    "web_app": [
        {
            "id": "BC-001",
            "name": "empty_form_submit",
            "description": "Submit form with all fields empty",
        },
        {
            "id": "BC-002",
            "name": "xss_in_input",
            "description": "Input field: <script>alert(1)</script>",
        },
        {
            "id": "BC-003",
            "name": "oversized_input",
            "description": "Text input with 100,000 characters",
        },
        {"id": "BC-004", "name": "path_traversal", "description": "URL path: /../../../etc/passwd"},
        {
            "id": "BC-005",
            "name": "unicode_in_url",
            "description": "URL with %00 null byte and RTL characters",
        },
        {"id": "BC-006", "name": "negative_quantity", "description": "Numeric input = -999"},
    ],
    "cli_tool": [
        {"id": "BC-001", "name": "no_arguments", "description": "Run CLI with zero arguments"},
        {
            "id": "BC-002",
            "name": "empty_string_arg",
            "description": "Pass empty string '' as argument",
        },
        {
            "id": "BC-003",
            "name": "path_traversal_arg",
            "description": "File path argument: ../../etc/passwd",
        },
        {
            "id": "BC-004",
            "name": "very_long_arg",
            "description": "Argument string with 10,000+ characters",
        },
        {
            "id": "BC-005",
            "name": "nonexistent_file",
            "description": "File argument pointing to nonexistent path",
        },
        {
            "id": "BC-006",
            "name": "special_chars",
            "description": "Argument containing shell metacharacters: ; | & $",
        },
    ],
    "library": [
        {
            "id": "BC-001",
            "name": "none_input",
            "description": "Pass None/null to all public functions",
        },
        {
            "id": "BC-002",
            "name": "empty_collections",
            "description": "Pass empty list/dict/string to collection args",
        },
        {
            "id": "BC-003",
            "name": "negative_numeric",
            "description": "Numeric arguments: -1, -MAX_INT",
        },
        {"id": "BC-004", "name": "max_int", "description": "Numeric arguments: 2^63-1"},
        {
            "id": "BC-005",
            "name": "wrong_type",
            "description": "Pass string where int expected and vice versa",
        },
        {
            "id": "BC-006",
            "name": "unicode_string",
            "description": "String with NUL byte, RTL override, emoji",
        },
    ],
    "mobile_android": [
        {
            "id": "BC-001",
            "name": "empty_intent_extras",
            "description": "Launch activity with no Intent extras",
        },
        {"id": "BC-002", "name": "null_bundle", "description": "savedInstanceState Bundle is null"},
        {
            "id": "BC-003",
            "name": "oversized_parcelable",
            "description": "Parcelable with 1MB+ data",
        },
        {
            "id": "BC-004",
            "name": "network_unavailable",
            "description": "API call with no network connectivity",
        },
        {
            "id": "BC-005",
            "name": "empty_db",
            "description": "App launched with empty/wiped database",
        },
    ],
    "desktop_app": [
        {"id": "BC-001", "name": "empty_file", "description": "Open empty file (0 bytes)"},
        {"id": "BC-002", "name": "oversized_file", "description": "Open file with 1GB+ content"},
        {
            "id": "BC-003",
            "name": "special_chars_path",
            "description": "File path with spaces, unicode, and special chars",
        },
        {
            "id": "BC-004",
            "name": "read_only_destination",
            "description": "Save to read-only directory",
        },
        {
            "id": "BC-005",
            "name": "concurrent_access",
            "description": "Two instances open same file simultaneously",
        },
    ],
}

_UNIVERSAL_CORPUS = [
    {
        "id": "BC-U01",
        "name": "unicode_bomb",
        "description": "Input with zero-width joiners and combining characters",
    },
    {
        "id": "BC-U02",
        "name": "format_string",
        "description": "Input containing %s %d %n format string specifiers",
    },
]


def _get_corpus(product_type: str) -> list[dict]:
    """Return product-type-specific corpus + universal items."""
    specific = _BOUNDARY_CORPUS.get(product_type, [])
    return specific + _UNIVERSAL_CORPUS


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


class BoundaryQAAgent(BaseQAAgent):
    """Tests boundary conditions using a deterministic product-type corpus."""

    @property
    def name(self) -> str:
        return "boundary_qa_agent"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        corpus = _get_corpus(request.product_type)
        corpus_json = json.dumps(corpus, ensure_ascii=False, indent=2)

        files = source_reader.read_for_analysis(request.source_path, request.product_type)
        if not files:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="boundary_analysis",
                    target=request.source_path,
                    outcome="skipped",
                    notes="No source files found for analysis",
                )
            )
            return session

        source_block = source_reader.format_for_prompt(files)

        prompt_loader.validate_builder_inputs(
            "qa/boundary_qa_agent",
            "analyze",
            {
                "project_goal": request.project_goal,
                "product_type": request.product_type,
                "source_files": source_block,
                "boundary_corpus_json": corpus_json,
            },
        )

        user_prompt = (
            f"project_goal: {request.project_goal}\n"
            f"product_type: {request.product_type}\n\n"
            f"Source files:\n{source_block}\n\n"
            f"Boundary test corpus ({len(corpus)} items):\n{corpus_json}\n\n"
            "Identify which boundary cases the implementation handles incorrectly."
        )

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="boundary_qa_agent_output",
                json_schema=ProbeAnalysisOutput.model_json_schema(),
            )
            output = ProbeAnalysisOutput.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("boundary_qa_agent_llm_failed exc=%s", exc)
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="boundary_analysis",
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
                probe_type="boundary_analysis",
                target=request.source_path,
                outcome=output.probe_outcome,
                findings_count=len(output.findings),
                notes=f"Corpus size: {len(corpus)}. {output.probe_summary}",
            )
        )

        logger.info(
            "boundary_qa_agent_complete project_id=%s corpus=%s findings=%s outcome=%s",
            request.project_id,
            len(corpus),
            len(output.findings),
            output.probe_outcome,
        )
        return session
