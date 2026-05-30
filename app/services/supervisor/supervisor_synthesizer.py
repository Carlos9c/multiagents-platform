"""
Cross-agent synthesis for the supervisor.

Receives all agent evaluation results and project context; produces a
prioritized narrative (critical → needs_attention → healthy) stored in
SupervisorReport.synthesis.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader

logger = logging.getLogger(__name__)

SUPERVISOR_SYNTHESIS_SYSTEM_PROMPT = prompt_loader.get("supervisor_synthesis")

_VERDICT_ORDER = {"degraded": 0, "needs_attention": 1, "healthy": 2}


class _SynthesisOutput(BaseModel):
    synthesis: str = Field(
        description="Full narrative synthesis in Markdown, ordered critical → low.",
        min_length=150,
    )


def synthesize(
    *,
    project_id: int,
    project_name: str,
    project_description: str,
    requirements_draft: str,
    overall_verdict: str,
    agent_results: list[dict],
) -> str:
    """Return a narrative synthesis string.

    agent_results is a flat list of dicts with keys:
        agent_name, verdict (str | None), findings, issues, suggestions
    """
    degraded = [a for a in agent_results if a.get("verdict") == "degraded"]
    needs_attention = [a for a in agent_results if a.get("verdict") == "needs_attention"]
    healthy_names = [a["agent_name"] for a in agent_results if a.get("verdict") == "healthy"]
    not_supervised_names = [a["agent_name"] for a in agent_results if a.get("verdict") is None]

    prompt_loader.validate_builder_inputs(
        "supervisor_synthesis",
        "main",
        {
            "project_id": project_id,
            "project_name": project_name,
            "project_description": project_description,
            "requirements_draft": requirements_draft,
            "overall_verdict": overall_verdict,
            "degraded_agents": degraded,
            "needs_attention_agents": needs_attention,
            "healthy_agents": healthy_names,
            "not_supervised_agents": not_supervised_names,
            "total_agent_count": len(agent_results),
        },
    )

    user_prompt = f"""
Synthesize the supervisor evaluation results for project {project_id}.

Project: {project_name}
Description: {project_description}

Requirements draft (gathered from user):
---
{requirements_draft or "(not available)"}
---

Overall verdict: {overall_verdict}

Degraded agents ({len(degraded)}):
{json.dumps(degraded, ensure_ascii=False, indent=2)}

Needs-attention agents ({len(needs_attention)}):
{json.dumps(needs_attention, ensure_ascii=False, indent=2)}

Healthy agents: {json.dumps(healthy_names)}

Not supervised (did not participate): {json.dumps(not_supervised_names)}

Total agents evaluated: {len(agent_results)}

Return a JSON object with a single field "synthesis" containing the full Markdown narrative.
""".strip()

    provider = get_llm_provider()
    schema = _SynthesisOutput.model_json_schema()

    raw = provider.generate_structured(
        system_prompt=SUPERVISOR_SYNTHESIS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="supervisor_synthesis_output",
        json_schema=schema,
    )

    output = _SynthesisOutput.model_validate(raw)
    return output.synthesis.strip()
