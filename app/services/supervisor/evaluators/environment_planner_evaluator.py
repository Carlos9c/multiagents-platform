"""
Supervisor evaluator for the environment_planner agent.

One LLM evaluation call per project, covering all planning_trace entries for
that agent (one initial call + zero or more repair calls).  If repair_count >= 2
the verdict is auto-degraded without an LLM call.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.project import Project
from app.services.llm.factory import get_llm_provider
from app.services.project_storage import ProjectStorageService
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.contracts import AgentEvaluationOutput, EvaluatorOutput
from app.services.supervisor.prompt_resolver import resolve_system_prompt
from app.services.supervisor.trace_writer import PLANNING_TRACE_FILENAME

logger = logging.getLogger(__name__)

ENVIRONMENT_PLANNER_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("environment_planner_evaluator")

_REPAIR_THRESHOLD_DEGRADED = 2  # repair_count >= this → auto-degrade

_storage = ProjectStorageService()


def _load_environment_planner_trace_entries(project_id: int) -> list[dict]:
    """Return all environment_planner trace entries from planning_trace.jsonl, in order."""
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME

    if not trace_path.exists():
        return []

    entries: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") == "environment_planner":
                entries.append(entry)
        except json.JSONDecodeError:
            logger.warning(
                "environment_planner_evaluator: invalid JSONL line for project %s", project_id
            )
    return entries


def _get_final_spec_snapshot(db: Session, project_id: int) -> dict:
    """Return the project's current runtime_spec dict, or an empty dict."""
    project = db.get(Project, project_id)
    if project is None or project.runtime_spec is None:
        return {}
    return dict(project.runtime_spec)


def _count_repairs(trace_entries: list[dict]) -> int:
    return sum(1 for e in trace_entries if e.get("call_type") == "repair")


def _load_catalog_trace_entry(project_id: int) -> dict:
    """Return the most recent catalog_selector trace entry, or {} if none found."""
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME

    if not trace_path.exists():
        return {}

    last_catalog_entry: dict = {}
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") == "catalog_selector":
                last_catalog_entry = entry
        except json.JSONDecodeError:
            pass
    return last_catalog_entry


def _build_user_prompt(
    *,
    project_id: int,
    system_prompt: str,
    trace_entries: list[dict],
    repair_count: int,
    final_spec_snapshot: dict,
    catalog_trace_entry: dict,
) -> str:
    prompt_loader.validate_builder_inputs(
        "environment_planner_evaluator",
        "main",
        {
            "project_id": project_id,
            "system_prompt": system_prompt,
            "trace_entries": trace_entries,
            "repair_count": repair_count,
            "final_spec_snapshot": final_spec_snapshot,
            "catalog_trace_entry": catalog_trace_entry,
        },
    )

    catalog_section = ""
    if catalog_trace_entry:
        catalog_section = f"""
Catalog selector context (what image was pre-selected before environment planning):
{json.dumps(catalog_trace_entry, ensure_ascii=False, indent=2)}
"""

    # Detect evolutionary project from trace inputs
    is_evolutionary = any(
        e.get("inputs", {}).get("has_existing_environment_context") is True
        for e in trace_entries
        if e.get("call_type") == "initial"
    )
    evolutionary_note = ""
    if is_evolutionary:
        evolutionary_note = """
Note: This is an EVOLUTIONARY project — the environment_planner received existing manifest file content
(has_existing_environment_context=true). Assess whether the planner correctly preserved pinned versions
from the existing manifests and ensured new dependencies are compatible with the existing stack.
"""

    return f"""
Evaluate the environment_planner agent's runtime spec for this project.

Project ID: {project_id}
Repair count: {repair_count}  (0 = initial spec passed smoke test; 1+ = repairs were needed)

System prompt that was active during environment planning:
---
{system_prompt}
---
{catalog_section}{evolutionary_note}
All planning trace entries (initial + any repairs):
{json.dumps(trace_entries, ensure_ascii=False, indent=2)}

Final RuntimeSpec stored on the project:
{json.dumps(final_spec_snapshot, ensure_ascii=False, indent=2)}

Instructions:
- Use repair_count as the primary quality signal: 0 = excellent, 1 = acceptable, 2+ = problematic.
- Assess whether the runtime_type and image are appropriate for the project's description and technology stack.
- Assess completeness and modernity of the dependency list relative to what the project description requires.
- For repair calls, assess whether the repair_rationale correctly diagnosed the root cause.
- If a catalog_selector entry is present, assess whether the planner built correctly on top of that selection.
  If the catalog chose the right image but repairs were still needed, the fault is in the dependency list.
- If this is an evolutionary project (see note above), assess preservation of existing pinned versions.
- Reference the runtime_type, image, and key dependency names in your findings.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "environment_planner_evaluator",
        "retry",
        {
            "project_id": project_id,
            "validation_error": validation_error,
        },
    )
    return f"""
Your previous response for project_id={project_id} was invalid.

Validation error:
{validation_error}

Correct the output and return valid JSON matching the schema.
Required fields: verdict (healthy|needs_attention|degraded), findings (string, min 20 chars), issues (list of strings), suggestions (list of strings).
""".strip()


class EnvironmentPlannerEvaluator:
    """Evaluates the environment_planner agent for a project."""

    AGENT_NAME = "environment_planner"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        trace_entries = _load_environment_planner_trace_entries(project_id)

        if not trace_entries:
            return EvaluatorOutput(result=None)

        repair_count = _count_repairs(trace_entries)

        # Auto-degrade without LLM when repair_count is excessive
        if repair_count >= _REPAIR_THRESHOLD_DEGRADED:
            return EvaluatorOutput(
                result=AgentEvaluationOutput(
                    verdict="degraded",
                    findings=(
                        f"Environment planner required {repair_count} repair calls before the "
                        f"smoke test passed. This indicates systematic miscalibration in the "
                        f"initial spec (wrong image, incompatible packages, or incorrect runtime_type)."
                    ),
                    issues=[
                        f"Initial spec failed smoke test {repair_count} time(s) — manual review of "
                        f"the planning_rationale and dependency list is recommended."
                    ],
                    suggestions=[
                        "Review the repair trace entries to identify recurring failure patterns.",
                        "Consider adding the project's technology stack to the catalog if not present.",
                    ],
                )
            )

        final_spec = _get_final_spec_snapshot(db, project_id)
        catalog_trace_entry = _load_catalog_trace_entry(project_id)
        system_prompt = resolve_system_prompt(
            "environment_planner",
            system_version=system_version,
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            system_prompt=system_prompt,
            trace_entries=trace_entries,
            repair_count=repair_count,
            final_spec_snapshot=final_spec,
            catalog_trace_entry=catalog_trace_entry,
        )

        raw = provider.generate_structured(
            system_prompt=ENVIRONMENT_PLANNER_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="environment_planner_evaluator_output",
            json_schema=AgentEvaluationOutput.model_json_schema(),
        )

        try:
            result = AgentEvaluationOutput.model_validate(raw)
        except ValidationError as exc:
            retry_prompt = _build_retry_prompt(
                project_id=project_id,
                validation_error=str(exc),
            )
            raw_retry = provider.generate_structured(
                system_prompt=ENVIRONMENT_PLANNER_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="environment_planner_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
