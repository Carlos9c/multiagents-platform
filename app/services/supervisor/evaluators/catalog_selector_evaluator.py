"""
Supervisor evaluator for the catalog_selector agent.

One LLM evaluation call per project, using the most recent catalog_selector
trace entry.  Compares the selected image against the final RuntimeSpec to
assess selection quality.
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

CATALOG_SELECTOR_EVALUATOR_SYSTEM_PROMPT = prompt_loader.get("catalog_selector_evaluator")

_storage = ProjectStorageService()


def _load_catalog_selector_trace_entries(project_id: int) -> list[dict]:
    """Return all catalog_selector trace entries from planning_trace.jsonl, in order."""
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
            if entry.get("agent") == "catalog_selector":
                entries.append(entry)
        except json.JSONDecodeError:
            logger.warning(
                "catalog_selector_evaluator: invalid JSONL line for project %s", project_id
            )
    return entries


def _get_final_runtime_info(db: Session, project_id: int) -> tuple[str | None, str | None]:
    """Return (runtime_type, image) from the project's final RuntimeSpec."""
    project = db.get(Project, project_id)
    if project is None or project.runtime_spec is None:
        return None, None
    spec = project.runtime_spec
    return spec.get("runtime_type"), spec.get("image")


def _load_environment_planner_context(
    project_id: int,
) -> tuple[int, list[dict]]:
    """Return (repair_count, trace_summary) from the environment_planner trace entries.

    trace_summary is a compact list of dicts showing call_type and key outputs,
    suitable for cross-reference in the catalog_selector evaluation prompt.
    """
    paths = _storage.get_project_paths(project_id)
    trace_path: Path = paths.project_meta_dir / PLANNING_TRACE_FILENAME

    if not trace_path.exists():
        return 0, []

    env_entries: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("agent") == "environment_planner":
                env_entries.append(entry)
        except json.JSONDecodeError:
            pass

    repair_count = sum(1 for e in env_entries if e.get("call_type") == "repair")

    summary: list[dict] = []
    for entry in env_entries:
        call_type = entry.get("call_type")
        output = entry.get("output_snapshot", {})
        item: dict = {"call_type": call_type}
        if call_type == "initial":
            item["runtime_type"] = output.get("runtime_type")
            item["image"] = output.get("image")
            item["dependencies_count"] = output.get("dependencies_count")
            item["planning_rationale_preview"] = (entry.get("reasoning") or "")[:200]
        else:  # repair
            item["corrected_image"] = output.get("corrected_image")
            item["corrected_dependencies_count"] = output.get("corrected_dependencies_count")
            item["repair_rationale_preview"] = (entry.get("reasoning") or "")[:200]
        summary.append(item)

    return repair_count, summary


def _build_user_prompt(
    *,
    project_id: int,
    system_prompt: str,
    trace_entry: dict,
    final_runtime_type: str | None,
    final_image: str | None,
    environment_planner_repair_count: int,
    environment_planner_trace_summary: list[dict],
) -> str:
    prompt_loader.validate_builder_inputs(
        "catalog_selector_evaluator",
        "main",
        {
            "project_id": project_id,
            "system_prompt": system_prompt,
            "trace_entry": trace_entry,
            "final_runtime_type": final_runtime_type,
            "final_image": final_image,
            "environment_planner_repair_count": environment_planner_repair_count,
            "environment_planner_trace_summary": environment_planner_trace_summary,
        },
    )

    downstream_section = ""
    if environment_planner_trace_summary:
        downstream_section = f"""
Downstream impact — environment_planner trace summary (repair_count={environment_planner_repair_count}):
{json.dumps(environment_planner_trace_summary, ensure_ascii=False, indent=2)}
"""

    return f"""
Evaluate the catalog_selector agent's image selection for this project.

Project ID: {project_id}

System prompt that was active during catalog selection:
---
{system_prompt}
---

Catalog selector trace entry:
{json.dumps(trace_entry, ensure_ascii=False, indent=2)}

Final RuntimeSpec (what was actually used after environment planning):
- runtime_type: {final_runtime_type or "(not set)"}
- image: {final_image or "(not set)"}
{downstream_section}
Instructions:
- Assess whether the selected_image matches the technology stack the project actually needed.
- Compare selected_image with final_image: large differences may indicate the selection was overridden.
- Evaluate the quality of the reasoning field: does it show genuine analysis of the project context?
- If selected_image is null, assess whether abstaining was appropriate given the project's description and technology stack.
- Note: catalog selection runs before task generation, so reasoning must be based on project description alone — evaluate accordingly.
- If environment_planner_repair_count > 0: assess whether the catalog selection may have contributed to
  the downstream repairs. If the catalog chose the correct technology, do not penalize it for dependency issues.
- Reference the selected_image, final_image, and reasoning content in your findings.
""".strip()


def _build_retry_prompt(*, project_id: int, validation_error: str) -> str:
    prompt_loader.validate_builder_inputs(
        "catalog_selector_evaluator",
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


class CatalogSelectorEvaluator:
    """Evaluates the catalog_selector agent for a project."""

    AGENT_NAME = "catalog_selector"

    def evaluate(
        self,
        *,
        db: Session,
        project_id: int,
        project_name: str = "",
        project_description: str = "",
        system_version: str | None = None,
    ) -> EvaluatorOutput:
        trace_entries = _load_catalog_selector_trace_entries(project_id)

        if not trace_entries:
            return EvaluatorOutput(result=None)

        # Use the most recent entry (normally only one per project)
        trace_entry = trace_entries[-1]

        final_runtime_type, final_image = _get_final_runtime_info(db, project_id)
        env_repair_count, env_trace_summary = _load_environment_planner_context(project_id)
        system_prompt = resolve_system_prompt(
            "catalog_selector",
            system_version=system_version,
        )
        provider = get_llm_provider()

        user_prompt = _build_user_prompt(
            project_id=project_id,
            system_prompt=system_prompt,
            trace_entry=trace_entry,
            final_runtime_type=final_runtime_type,
            final_image=final_image,
            environment_planner_repair_count=env_repair_count,
            environment_planner_trace_summary=env_trace_summary,
        )

        raw = provider.generate_structured(
            system_prompt=CATALOG_SELECTOR_EVALUATOR_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="catalog_selector_evaluator_output",
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
                system_prompt=CATALOG_SELECTOR_EVALUATOR_SYSTEM_PROMPT,
                user_prompt=retry_prompt,
                schema_name="catalog_selector_evaluator_output",
                json_schema=AgentEvaluationOutput.model_json_schema(),
            )
            result = AgentEvaluationOutput.model_validate(raw_retry)

        return EvaluatorOutput(result=result)
