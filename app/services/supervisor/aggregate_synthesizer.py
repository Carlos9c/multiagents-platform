"""
LLM synthesis for the aggregate supervisor report.

Receives the frequency table and text corpus; produces a cross-project
narrative ordered by severity (critical patterns → recurring issues → proposals).
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

AGGREGATE_SYNTHESIZER_SYSTEM_PROMPT = prompt_loader.get("aggregate_synthesizer")


class _AggregateSynthesisOutput(BaseModel):
    synthesis: str = Field(
        description="Full narrative synthesis in Markdown, ordered by severity.",
        min_length=200,
    )


def synthesize(
    *,
    project_count: int,
    filter_description: str,
    frequency_table: list[dict],
    text_corpus: str,
) -> str:
    """Call the LLM and return the narrative synthesis string."""
    prompt_loader.validate_builder_inputs(
        "aggregate_synthesizer",
        "main",
        {
            "project_count": project_count,
            "filter_description": filter_description,
            "frequency_table": frequency_table,
            "text_corpus": text_corpus,
        },
    )

    user_prompt = f"""
Analyze {project_count} supervisor reports ({filter_description}).

Per-agent verdict frequency table (sorted by degradation rate):
{json.dumps(frequency_table, ensure_ascii=False, indent=2)}

Evidence corpus from non-healthy evaluations (degraded first, then needs_attention):
---
{text_corpus}
---

Return a JSON object with a single field "synthesis" containing the full Markdown narrative.
""".strip()

    provider = get_llm_provider()
    raw = provider.generate_structured(
        system_prompt=AGGREGATE_SYNTHESIZER_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="aggregate_synthesizer_output",
        json_schema=_AggregateSynthesisOutput.model_json_schema(),
    )

    output = _AggregateSynthesisOutput.model_validate(raw)
    return output.synthesis.strip()
