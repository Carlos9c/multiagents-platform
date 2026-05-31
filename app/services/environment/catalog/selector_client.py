"""LLM-based catalog image selector.

Replaces the previous keyword-matching approach with a structured LLM call that
understands project context and selects the most appropriate curated base image.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from app.services.environment.catalog.registry import CatalogEntry
from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.supervisor.trace_writer import append_planning_trace

logger = logging.getLogger(__name__)

CATALOG_SELECTOR_SYSTEM_PROMPT = prompt_loader.get("catalog_selector")


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
    entries: list[CatalogEntry],
) -> str:
    from app.services.prompt_loader import prompt_loader

    catalog_text = _build_catalog_text(entries)
    prompt_loader.validate_builder_inputs(
        "catalog_selector",
        "main",
        {
            "project_name": project_name,
            "project_description": project_description,
            "available_catalog_images": entries,
        },
    )
    return f"""Project name: {project_name}

Project description:
{project_description}

Available catalog images:
{catalog_text}

Select the image_name that best fits this project, or return null if none fits well.
Return the EXACT image_name string as it appears above."""


def select_catalog_image(
    project_name: str,
    project_description: str,
    entries: list[CatalogEntry],
    project_id: int | None = None,
) -> CatalogSelectionOutput:
    provider = get_llm_provider()

    user_prompt = _build_selector_user_prompt(
        project_name=project_name,
        project_description=project_description,
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

    if project_id is not None:
        append_planning_trace(
            project_id=project_id,
            entry={
                "agent": "catalog_selector",
                "call_type": "initial",
                "project_id": project_id,
                "inputs": {
                    "project_name": project_name,
                    "catalog_images_count": len(entries),
                },
                "reasoning": output.reasoning,
                "output_snapshot": {
                    "selected_image": output.selected_image,
                },
            },
        )

    return output
