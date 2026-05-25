"""LLM-based catalog image selector.

Replaces the previous keyword-matching approach with a structured LLM call that
understands project context and selects the most appropriate curated base image.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from app.services.environment.catalog.registry import CatalogEntry
from app.services.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)

CATALOG_SELECTOR_SYSTEM_PROMPT = """
You are an environment selection agent for an autonomous software development system.

Your job: given a software project description and a catalog of available Docker base images,
select the single most appropriate image for the project.

Selection rules:
- Match the image whose ecosystem covers the project's PRIMARY language and framework.
- For hybrid projects (e.g. React Native needs Node + Android SDK; a Python API with a
  React frontend in the same repo needs the py-node fullstack image), select the hybrid
  image when one is available in the catalog.
- For standard single-ecosystem projects (pure Python, pure Node, pure Java, pure Rust,
  pure Go, pure .NET, native Android, Flutter), select the matching single-ecosystem image.
- Return the EXACT image_name string from the catalog — do not modify it, do not invent names.
- Return null if no catalog entry is a good fit (e.g. an unusual runtime not in the catalog,
  or a multi-language combination that no catalog entry covers). The system will then select
  an appropriate public image automatically.
- When two images could work, prefer the more specific one (e.g. Flutter over Android for
  Flutter projects; React Native over Node for React Native projects).

Return JSON matching the schema. Always populate the reasoning field.
""".strip()


class CatalogSelectionOutput(BaseModel):
    selected_image: str | None
    reasoning: str


def _build_catalog_text(entries: list[CatalogEntry]) -> str:
    lines = []
    for entry in entries:
        lines.append(f'- image_name: "{entry.image_name}"')
        lines.append(f"  runtime_type: {entry.runtime_type}")
        lines.append(f"  description: {entry.description}")
    return "\n".join(lines)


def _build_selector_user_prompt(
    project_name: str,
    project_description: str,
    task_titles: list[str],
    entries: list[CatalogEntry],
) -> str:
    catalog_text = _build_catalog_text(entries)
    tasks_text = "\n".join(f"  - {t}" for t in task_titles[:15]) or "  (no tasks yet)"
    return f"""Project name: {project_name}

Project description:
{project_description}

Atomic task titles (sample):
{tasks_text}

Available catalog images:
{catalog_text}

Select the image_name that best fits this project, or return null if none fits well.
Return the EXACT image_name string as it appears above."""


def select_catalog_image(
    project_name: str,
    project_description: str,
    task_dicts: list[dict],
    entries: list[CatalogEntry],
) -> CatalogSelectionOutput:
    provider = get_llm_provider()
    task_titles = [t.get("title", "") for t in task_dicts if t.get("title")]

    user_prompt = _build_selector_user_prompt(
        project_name=project_name,
        project_description=project_description,
        task_titles=task_titles,
        entries=entries,
    )

    raw = provider.generate_structured(
        system_prompt=CATALOG_SELECTOR_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema_name="catalog_selection",
        json_schema=CatalogSelectionOutput.model_json_schema(),
    )

    output = CatalogSelectionOutput.model_validate(raw)

    logger.info(
        "catalog_selector selected_image=%s reasoning=%s",
        output.selected_image,
        output.reasoning[:120],
    )

    return output
